"""Slow sync work (Authentik, Quotient) must not hold the one thread every other sync_to_async call queues on."""

import asyncio
import threading
from collections.abc import Awaitable, Callable, Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from asgiref.sync import sync_to_async


async def _shared_thread_answers_while(start: Callable[[Callable[..., Any]], Awaitable[object]]) -> bool:
    """Whether a plain sync_to_async call completes while the work `start` runs is blocked in its sync part."""
    entered, release = threading.Event(), threading.Event()

    def blocked(*_args: object, **_kwargs: object) -> Any:
        entered.set()
        release.wait(10)
        return MagicMock()

    work = asyncio.ensure_future(start(blocked))
    try:
        await asyncio.to_thread(entered.wait, 5)
        assert entered.is_set(), "the work never reached its sync part"
        try:
            await asyncio.wait_for(sync_to_async(lambda: None)(), timeout=1)
        except TimeoutError:
            return False
        return True
    finally:
        release.set()
        await asyncio.gather(work, return_exceptions=True)


async def _group_refresh(blocked: Callable[..., Any]) -> None:
    from bot.cogs import authentik_groups

    with patch.object(authentik_groups, "refresh_user_groups", blocked):
        await authentik_groups.refresh_groups_now()


async def _quotient_sync(blocked: Callable[..., Any]) -> None:
    from bot.cogs import quotient_sync

    cog = quotient_sync.QuotientSyncCog.__new__(quotient_sync.QuotientSyncCog)
    cog.quotient_unavailable = False
    with patch.object(quotient_sync, "sync_quotient_metadata", blocked):
        await cog.run_sync()


def _interaction() -> AsyncMock:
    interaction = AsyncMock(spec=discord.Interaction)
    interaction.response, interaction.followup = AsyncMock(), AsyncMock()
    interaction.created_at = discord.utils.utcnow()
    return interaction


def _one_batch(blocked: Callable[..., Any]) -> Generator[object, None, object]:
    yield blocked()
    return MagicMock(team_numbers=[1])


async def _bulk_reset(blocked: Callable[..., Any]) -> None:
    from bot.cogs import admin_competition

    interaction = _interaction()
    cog = admin_competition.AdminCompetitionCog(MagicMock())
    with (
        patch.object(admin_competition, "reset_teams_credentials", lambda *_a: _one_batch(blocked)),
        patch.object(admin_competition, "settings", MagicMock(AUTHENTIK_TOKEN="t")),
    ):
        await cog.admin_reset_blueteam_passwords.callback(cog, interaction, team_numbers="1")


async def _ticket_command(blocked: Callable[..., Any]) -> None:
    from bot.cogs import ticketing

    cog = ticketing.TicketingCog.__new__(ticketing.TicketingCog)
    member = MagicMock()
    with (
        patch.object(ticketing, "linked_team_member", AsyncMock(return_value=member)),
        patch.object(ticketing, "file_ticket", blocked),
    ):
        await ticketing.TicketingCog.create_ticket.callback(cog, _interaction(), category="2", description="x")


async def _help_panel_ticket(blocked: Callable[..., Any]) -> None:
    from bot.cogs import help_panels

    with (
        patch.object(help_panels, "linked_team_member", AsyncMock(return_value=MagicMock())),
        patch.object(help_panels, "file_ticket", blocked),
    ):
        await help_panels.create_ticket(_interaction(), category_id="2", description="x")


async def _admin_ticket_command(blocked: Callable[..., Any]) -> None:
    from bot.cogs import admin_tickets

    cog = admin_tickets.AdminTicketsCog(MagicMock())
    with (
        patch.object(admin_tickets, "find_team", AsyncMock(return_value=MagicMock())),
        patch.object(admin_tickets, "file_ticket", blocked),
    ):
        await admin_tickets.AdminTicketsCog.admin_ticket_create.callback(
            cog, _interaction(), team_number=1, category="2", description="x"
        )


async def _team_reset(blocked: Callable[..., Any]) -> None:
    from bot.cogs import admin_teams
    from team.models import Team

    await Team.objects.aget_or_create(team_number=1, defaults={"team_name": "Team 01"})
    cog = admin_teams.AdminTeamsCog(MagicMock())
    with (
        patch.object(admin_teams, "unlink_team_members", AsyncMock(return_value=0)),
        patch.object(admin_teams, "reset_team_credentials", blocked),
    ):
        await admin_teams.AdminTeamsCog.admin_reset_team.callback(
            cog, _interaction(), team_number=1, recreate_channels=False
        )


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "start",
    [
        _group_refresh,
        _quotient_sync,
        _bulk_reset,
        _ticket_command,
        _help_panel_ticket,
        _admin_ticket_command,
        _team_reset,
    ],
    ids=[
        "group-refresh",
        "quotient-sync",
        "bulk-reset",
        "ticket",
        "help-panel-ticket",
        "admin-ticket",
        "team-reset",
    ],
)
async def test_slow_sync_work_leaves_the_shared_thread_free(start) -> None:
    assert await _shared_thread_answers_while(start)
