"""Bot ticket commands go through the locked lifecycle helpers."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from django.utils import timezone

from bot.cogs.admin_tickets import AdminTicketsCog
from bot.cogs.ticketing import TicketingCog
from bot.tests.discord_fakes import competition_thread
from bot.ticket_dashboard import ResolveTicketModal, TicketActionView
from core.models import DiscordTask
from team.models import Team
from ticketing.models import Ticket, TicketCategory, TicketHistory

pytestmark = [pytest.mark.asyncio, pytest.mark.django_db(transaction=True)]


@pytest.fixture
async def team(db: Any) -> Team:
    return await Team.objects.acreate(team_number=44, team_name="Team 44")


async def _ticket(team: Team, category: TicketCategory, status: str, **extra: Any) -> Ticket:
    return await Ticket.objects.acreate(
        ticket_number=f"T044-{status}", team=team, category=category, title="t", status=status, **extra
    )


async def test_resolve_refuses_cancelled_ticket(
    team: Team, box_reset_category: TicketCategory, mock_interaction: Any, mock_bot: Any
) -> None:
    ticket = await _ticket(team, box_reset_category, "cancelled")
    cog = AdminTicketsCog(mock_bot)
    await cog.admin_ticket_resolve.callback(cog, mock_interaction, ticket_number=ticket.ticket_number)

    assert "Cannot resolve ticket with status: cancelled" in mock_interaction.response.send_message.await_args.args[0]
    await ticket.arefresh_from_db()
    assert (ticket.status, ticket.points_charged) == ("cancelled", 0)


async def test_admin_cancels_claimed_ticket(
    team: Team, box_reset_category: TicketCategory, mock_interaction: Any, mock_bot: Any
) -> None:
    ticket = await _ticket(team, box_reset_category, "claimed", discord_thread_id=123)
    cog = AdminTicketsCog(mock_bot)
    with patch("bot.cogs.admin_tickets.log_to_ops_channel", new_callable=AsyncMock):
        await cog.admin_ticket_cancel.callback(cog, mock_interaction, ticket_number=ticket.ticket_number, reason="dup")

    await ticket.arefresh_from_db()
    assert (ticket.status, ticket.resolution_notes) == ("cancelled", "dup")
    assert ticket.thread_archive_scheduled_at is not None


async def test_archive_loop_does_not_undo_a_reopen(team: Team, box_reset_category: TicketCategory) -> None:
    ticket = await _ticket(
        team, box_reset_category, "resolved", discord_thread_id=321, thread_archive_scheduled_at=timezone.now()
    )

    async def reopen_while_archiving(**_: Any) -> None:
        await Ticket.objects.filter(pk=ticket.pk).aupdate(status="open")

    thread = competition_thread()
    thread.edit = AsyncMock(side_effect=reopen_while_archiving)
    bot = MagicMock()
    bot.get_channel.return_value = thread
    with patch.object(TicketingCog.archive_threads_task, "start"):
        cog = TicketingCog(bot)
    await cog.archive_threads_task.coro(cog)

    await ticket.arefresh_from_db()
    assert ticket.status == "open"
    assert ticket.thread_archive_scheduled_at is None


def _bot_client() -> tuple[MagicMock, MagicMock]:
    thread = competition_thread()
    thread.add_user = AsyncMock()
    client = MagicMock()
    client.unified_dashboard = None
    client.get_channel.return_value = thread
    client.fetch_channel = AsyncMock(return_value=thread)
    return client, thread


async def _effects(ticket: Ticket) -> tuple[list[str], list[str], list[int]]:
    """(history actions, post_ticket_update actions, discord ids invited to the thread)."""
    history = [h.action async for h in TicketHistory.objects.filter(ticket=ticket).order_by("pk")]
    tasks = [t async for t in DiscordTask.objects.filter(ticket=ticket).order_by("pk")]
    return (
        history,
        [t.payload["action"] for t in tasks if t.task_type == "post_ticket_update"],
        [t.payload["discord_id"] for t in tasks if t.task_type == "add_user_to_thread"],
    )


async def test_claim_button_leaves_thread_join_to_the_queue(
    team: Team, box_reset_category: TicketCategory, mock_interaction: Any, mock_admin_user: Any
) -> None:
    ticket = await _ticket(team, box_reset_category, "open", discord_thread_id=777)
    mock_interaction.user.id = mock_admin_user._discord_id
    mock_interaction.client, thread = _bot_client()

    await TicketActionView(ticket.id).claim_button.callback(mock_interaction)

    assert await _effects(ticket) == (["claimed"], ["claimed"], [mock_admin_user._discord_id])
    thread.add_user.assert_not_awaited()


async def test_resolve_modal_queues_one_update(
    team: Team, box_reset_category: TicketCategory, mock_interaction: Any, mock_admin_user: Any
) -> None:
    ticket = await _ticket(team, box_reset_category, "claimed")
    mock_interaction.user.id = mock_admin_user._discord_id
    mock_interaction.client, _ = _bot_client()
    modal = ResolveTicketModal(ticket)
    modal.points._value = ""

    await modal.on_submit(mock_interaction)

    assert await _effects(ticket) == (["resolved"], ["resolved"], [])


async def test_resolve_button_refuses_cancelled_ticket_before_the_modal(
    team: Team, box_reset_category: TicketCategory, mock_interaction: Any, mock_admin_user: Any
) -> None:
    ticket = await _ticket(team, box_reset_category, "cancelled")
    mock_interaction.user.id = mock_admin_user._discord_id

    await TicketActionView(ticket.id).resolve_button.callback(mock_interaction)

    mock_interaction.response.send_modal.assert_not_awaited()
    mock_interaction.response.send_message.assert_awaited_once_with(
        "Cannot resolve ticket with status: cancelled.", ephemeral=True
    )


async def test_cancel_button_queues_one_update(
    team: Team, box_reset_category: TicketCategory, mock_interaction: Any, mock_admin_user: Any
) -> None:
    ticket = await _ticket(team, box_reset_category, "open")
    mock_interaction.user.id = mock_admin_user._discord_id
    mock_interaction.client, _ = _bot_client()

    await TicketActionView(ticket.id).cancel_button.callback(mock_interaction)

    assert await _effects(ticket) == (["cancelled"], ["cancelled"], [])


async def test_reopen_command_queues_one_update(
    team: Team, box_reset_category: TicketCategory, mock_interaction: Any, mock_bot: Any
) -> None:
    ticket = await _ticket(team, box_reset_category, "resolved", points_charged=5)
    cog = AdminTicketsCog(mock_bot)
    with patch("bot.cogs.admin_tickets.log_to_ops_channel", new_callable=AsyncMock):
        await cog.admin_ticket_reopen.callback(cog, mock_interaction, ticket_number=ticket.ticket_number, reason="x")

    assert await _effects(ticket) == (["reopened"], ["reopened"], [])


async def test_reassign_command_leaves_thread_join_to_the_queue(
    team: Team, box_reset_category: TicketCategory, mock_interaction: Any, mock_bot: Any, mock_admin_user: Any
) -> None:
    ticket = await _ticket(team, box_reset_category, "open", discord_thread_id=778)
    volunteer = MagicMock(spec=discord.User)
    volunteer.id = mock_admin_user._discord_id
    thread = competition_thread()
    thread.add_user = AsyncMock()
    mock_interaction.guild.get_thread.return_value = thread

    cog = AdminTicketsCog(mock_bot)
    with patch("bot.cogs.admin_tickets.log_to_ops_channel", new_callable=AsyncMock):
        await cog.admin_ticket_reassign.callback(
            cog, mock_interaction, ticket_number=ticket.ticket_number, volunteer=volunteer
        )

    assert await _effects(ticket) == (["claimed"], ["assigned"], [mock_admin_user._discord_id])
    thread.add_user.assert_not_awaited()
