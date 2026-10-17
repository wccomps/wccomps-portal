"""The assignee rule holds in Discord too: a claimed ticket is resolved, unclaimed, recategorized or given away only
by its assignee or a ticketing admin (ticketing.lifecycle.assignee_refusal, checked under the row lock)."""

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth.models import User
from django.utils import timezone

from bot.cogs.admin_tickets import AdminTicketsCog
from bot.ticket_dashboard import ResolveTicketModal, TicketActionView
from core.tests.roles import make_user
from team.models import DiscordLink, Team
from ticketing.models import Ticket, TicketCategory

pytestmark = [pytest.mark.asyncio, pytest.mark.django_db(transaction=True)]


async def _support(username: str) -> tuple[User, int]:
    """A ticketing support member (not an admin) linked on Discord, and their Discord id."""
    user = await sync_to_async(make_user)(username, ["WCComps_Ticketing_Support"])
    link = await DiscordLink.objects.aget(user=user)
    return user, link.discord_id


@pytest.fixture
async def held(db: Any, box_reset_category: TicketCategory) -> Ticket:
    team = await Team.objects.acreate(team_number=45, team_name="Team 45")
    holder, _ = await _support("holder45")
    return await Ticket.objects.acreate(
        ticket_number="T045-001",
        team=team,
        category=box_reset_category,
        title="t",
        status="claimed",
        assigned_to=holder,
        assigned_at=timezone.now(),
    )


async def test_resolve_button_refuses_a_non_assignee_naming_who_holds_it(held: Ticket, mock_interaction: Any) -> None:
    _, discord_id = await _support("other45")
    mock_interaction.user.id = discord_id

    await TicketActionView(held.id).resolve_button.callback(mock_interaction)

    mock_interaction.response.send_modal.assert_not_awaited()
    said = mock_interaction.response.send_message.await_args.args[0]
    assert "holder45" in said


async def test_resolve_modal_refuses_a_non_assignee_under_the_lock(held: Ticket, mock_interaction: Any) -> None:
    """The button's check can go stale; the modal's submit goes through the lifecycle, which checks again."""
    _, discord_id = await _support("other45")
    mock_interaction.user.id = discord_id
    modal = ResolveTicketModal(held, {"points": 5})
    modal.points._value = ""

    await modal.on_submit(mock_interaction)

    assert "holder45" in mock_interaction.response.send_message.await_args.args[0]
    await held.arefresh_from_db()
    assert held.status == "claimed"


async def test_resolve_command_refuses_a_non_assignee(held: Ticket, mock_interaction: Any, mock_bot: Any) -> None:
    _, discord_id = await _support("other45")
    mock_interaction.user.id = discord_id
    cog = AdminTicketsCog(mock_bot)
    await cog.admin_ticket_resolve.callback(cog, mock_interaction, ticket_number=held.ticket_number)

    assert "holder45" in mock_interaction.response.send_message.await_args.args[0]
    await held.arefresh_from_db()
    assert held.status == "claimed"


async def test_a_ticketing_admin_resolves_someone_elses_ticket(
    held: Ticket, mock_interaction: Any, mock_admin_user: Any
) -> None:
    mock_interaction.user.id = mock_admin_user._discord_id
    await TicketActionView(held.id).resolve_button.callback(mock_interaction)
    mock_interaction.response.send_modal.assert_awaited_once()

    modal = ResolveTicketModal(held, {"points": 5})
    modal.points._value = ""
    await modal.on_submit(mock_interaction)
    await held.arefresh_from_db()
    assert held.status == "resolved"


async def test_a_ticketing_admin_reassigns_and_unclaims_someone_elses_ticket(
    held: Ticket, mock_interaction: Any, mock_bot: Any, mock_admin_user: Any
) -> None:
    mock_interaction.user.id = mock_admin_user._discord_id
    cog = AdminTicketsCog(mock_bot)
    with patch("bot.cogs.admin_tickets.log_to_ops_channel", new_callable=AsyncMock):
        await cog.admin_ticket_reassign.callback(cog, mock_interaction, ticket_number=held.ticket_number)
    await held.arefresh_from_db()
    assert (held.status, held.assigned_to_id) == ("open", None)
