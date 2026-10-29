"""Slow sync work (Authentik, Quotient) must not hold the one thread every other sync_to_async call queues on."""

import asyncio
import threading
from collections.abc import Awaitable, Callable
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
    return interaction


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
        _team_reset,
    ],
    ids=[
        "group-refresh",
        "quotient-sync",
        "team-reset",
    ],
)
async def test_slow_sync_work_leaves_the_shared_thread_free(start) -> None:
    assert await _shared_thread_answers_while(start)
