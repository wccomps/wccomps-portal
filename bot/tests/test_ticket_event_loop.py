"""Ticket paths that run on the bot's event loop, with Django's async-safety check on."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from django.contrib.auth.models import User

from bot.cogs.admin_tickets import AdminTicketsCog
from bot.discord_queue import DiscordQueueProcessor
from bot.ticket_dashboard import ResolveTicketModal, TicketActionView
from core.models import DiscordTask
from team.models import Team
from ticketing.models import Ticket, TicketCategory

pytestmark = [pytest.mark.asyncio, pytest.mark.django_db(transaction=True)]


@pytest.fixture
async def claimed_ticket(db: Any) -> Ticket:
    team = await Team.objects.acreate(team_number=21, team_name="Team 21")
    category = await TicketCategory.objects.acreate(
        pk=90, display_name="Variable", points=0, variable_points=True, min_points=5, max_points=50, sort_order=90
    )
    volunteer = await User.objects.acreate(username="volunteer21")
    return await Ticket.objects.acreate(
        ticket_number="T021-001",
        team=team,
        category=category,
        title="Variable",
        status="claimed",
        assigned_to=volunteer,
    )


async def test_list_claimed_tickets_shows_assignee(
    claimed_ticket: Ticket, mock_interaction: Any, mock_bot: Any
) -> None:
    cog = AdminTicketsCog(mock_bot)
    await cog.admin_ticket_list.callback(cog, mock_interaction, status="claimed")

    embed = mock_interaction.response.send_message.await_args.kwargs["embed"]
    assert "Assigned: volunteer21" in embed.fields[0].value


async def test_unassign_claimed_ticket(claimed_ticket: Ticket, mock_interaction: Any, mock_bot: Any) -> None:
    cog = AdminTicketsCog(mock_bot)
    with patch("bot.cogs.admin_tickets.log_to_ops_channel", new_callable=AsyncMock):
        await cog.admin_ticket_reassign.callback(cog, mock_interaction, ticket_number="T021-001", volunteer=None)

    reply = mock_interaction.followup.send.await_args.args[0]
    assert "From: volunteer21" in reply
    await claimed_ticket.arefresh_from_db()
    assert claimed_ticket.status == "open"


async def test_resolve_button_opens_modal_and_rejects_out_of_range_points(
    claimed_ticket: Ticket, mock_interaction: Any
) -> None:
    view = TicketActionView(claimed_ticket.id)
    with patch("bot.permissions.has_permission", new=AsyncMock(return_value=True)):
        await view.resolve_button.callback(mock_interaction)

    modal = mock_interaction.response.send_modal.await_args.args[0]
    assert isinstance(modal, ResolveTicketModal)
    assert modal.points.placeholder == "Enter points (5-50)"

    modal.points._value = "500"
    submit = AsyncMock(spec=discord.Interaction)
    submit.user = mock_interaction.user
    submit.response = AsyncMock()
    await modal.on_submit(submit)

    assert submit.response.send_message.await_args.args[0] == "Point value must be at most 50."
    await claimed_ticket.arefresh_from_db()
    assert claimed_ticket.status == "claimed"


async def test_updates_for_threadless_ticket_complete_and_refresh_dashboard(claimed_ticket: Ticket) -> None:
    bot = MagicMock(spec=discord.Client)
    bot.unified_dashboard = MagicMock()
    bot.unified_dashboard.trigger_update = MagicMock()
    processor = DiscordQueueProcessor(bot)

    update = await DiscordTask.objects.acreate(
        task_type="post_ticket_update",
        ticket=claimed_ticket,
        payload={"ticket_id": claimed_ticket.id, "action": "claimed", "actor": "v"},
    )
    await processor._handle_post_ticket_update(update.typed_payload())
    bot.unified_dashboard.trigger_update.assert_called_once()

    comment = await claimed_ticket.comments.acreate(comment_text="hi")
    task = await DiscordTask.objects.acreate(
        task_type="post_comment", payload={"ticket_id": claimed_ticket.id, "comment_id": comment.id}
    )
    await processor._handle_post_comment(task.typed_payload())


@pytest.mark.parametrize(("action", "unlocks"), [("reopened", True), ("claimed", False)])
async def test_reopen_unlocks_the_archived_thread(claimed_ticket: Ticket, action: str, unlocks: bool) -> None:
    """Resolving archives and locks the thread; a team can't post in it again until the reopen unlocks it."""
    claimed_ticket.discord_thread_id = 5551
    await claimed_ticket.asave(update_fields=["discord_thread_id"])
    thread = MagicMock(spec=discord.Thread)
    thread.archived = True
    thread.locked = True
    calls: list[str] = []
    thread.edit = AsyncMock(side_effect=lambda **kwargs: calls.append(f"edit {kwargs}"))
    thread.send = AsyncMock(side_effect=lambda *args, **kwargs: calls.append("send"))
    bot = MagicMock(spec=discord.Client)
    bot.unified_dashboard = MagicMock()
    bot.get_channel.return_value = thread
    processor = DiscordQueueProcessor(bot)

    task = await DiscordTask.objects.acreate(
        task_type="post_ticket_update",
        ticket=claimed_ticket,
        payload={"ticket_id": claimed_ticket.id, "action": action, "actor": "v"},
    )
    await processor._handle_post_ticket_update(task.typed_payload())

    expected = ["edit {'archived': False, 'locked': False}", "send"] if unlocks else ["send"]
    assert calls == expected
