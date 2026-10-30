"""Ticket thread buttons keep working across restarts and reconnects without re-editing any message."""

from typing import Any
from unittest.mock import MagicMock

import discord
import pytest
from asgiref.sync import sync_to_async

from bot.ticket_dashboard import TicketActionView, format_ticket_embed
from team.models import Team
from ticketing.models import Ticket, TicketCategory

pytestmark = [pytest.mark.asyncio, pytest.mark.django_db(transaction=True)]


def _custom_ids(view: discord.ui.View) -> set[str]:
    return {item.custom_id for item in view.children if isinstance(item, discord.ui.Button) and item.custom_id}


async def test_posted_buttons_are_routed_to_the_registered_view() -> None:
    """Discord routes a click by custom_id, so the view main.py registers must cover every posted button."""
    registered = TicketActionView(ticket_id=0)
    posted = TicketActionView(ticket_id=123)

    assert registered.is_persistent()
    assert _custom_ids(posted) and _custom_ids(posted) == _custom_ids(registered)


async def test_registered_view_finds_the_ticket_from_the_thread_channel(db: Any) -> None:
    team = await Team.objects.acreate(team_number=22, team_name="Team 22")
    category = await TicketCategory.objects.acreate(pk=91, display_name="Box", sort_order=91)
    ticket = await Ticket.objects.acreate(
        ticket_number="T022-004",
        team=team,
        category=category,
        title="Web down: port 80",
        status="open",
        discord_thread_id=888_004,
    )
    ticket = await Ticket.objects.select_related("team", "assigned_to").aget(pk=ticket.pk)
    interaction = MagicMock(spec=discord.Interaction)
    interaction.channel.id = 888_004
    interaction.message.embeds = [await sync_to_async(format_ticket_embed)(ticket)]

    assert await TicketActionView(ticket_id=0)._get_ticket_id_from_interaction(interaction) == ticket.id


async def test_ops_can_cancel_a_claimed_ticket_from_the_thread(db: Any) -> None:
    """The thread's Cancel button must pass staff, as /tickets cancel does; teams can cancel only open ones."""
    from unittest.mock import AsyncMock, patch

    team = await Team.objects.acreate(team_number=23, team_name="Team 23")
    category = await TicketCategory.objects.acreate(pk=92, display_name="Box", sort_order=92)
    ticket = await Ticket.objects.acreate(
        ticket_number="T023-001",
        team=team,
        category=category,
        title="Box",
        status="claimed",
        discord_thread_id=888_005,
    )
    ticket = await Ticket.objects.select_related("team", "assigned_to").aget(pk=ticket.pk)
    interaction = MagicMock(spec=discord.Interaction)
    interaction.user.id = 4242
    interaction.channel.id = 888_005
    interaction.message.embeds = [await sync_to_async(format_ticket_embed)(ticket)]
    interaction.response = AsyncMock()
    view = TicketActionView(ticket_id=0)

    with patch("bot.permissions.ticket_access", AsyncMock(return_value=(True, None))):
        await view.cancel_button.callback(interaction)

    await ticket.arefresh_from_db()
    assert ticket.status == "cancelled"
