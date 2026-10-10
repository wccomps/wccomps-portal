"""run_competition_cleanup: the one teardown behind the slash command and the ops page."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from django.contrib.auth.models import User
from django.utils import timezone
from scoring.models import QuotientMetadataCache

from bot.competition_actions import run_competition_cleanup
from bot.discord_queue import DiscordQueueProcessor
from core.models import AuditLog, CompetitionConfig, DiscordTask
from team.models import DiscordLink, Team

pytestmark = [pytest.mark.asyncio, pytest.mark.django_db(transaction=True)]


def _guild(*category_names: str) -> MagicMock:
    guild = MagicMock(spec=discord.Guild)
    categories = []
    for name in category_names:
        category = MagicMock(spec=discord.CategoryChannel)
        category.name = name
        category.channels = [AsyncMock(spec=discord.TextChannel)]
        category.delete = AsyncMock()
        categories.append(category)
    guild.categories = categories
    guild.roles = []
    guild.get_role.return_value = None
    return guild


@pytest.fixture
async def competition(db: Any) -> Team:
    await CompetitionConfig.objects.aupdate_or_create(
        pk=1,
        defaults={
            "applications_enabled": False,
            "competition_start_time": timezone.now(),
            "start_message": "Default VM login",
        },
    )
    team = await Team.objects.acreate(team_number=3, team_name="Team 03", discord_role_id=11, discord_category_id=22)
    member = await User.objects.acreate(username="member03")
    staff = await User.objects.acreate(username="staff")
    await DiscordLink.objects.acreate(user=member, discord_id=1, discord_username="m", team=team, is_active=True)
    await DiscordLink.objects.acreate(user=staff, discord_id=2, discord_username="s", is_active=True)
    await QuotientMetadataCache.objects.acreate()
    return team


async def test_tears_down_discord_and_database(competition: Team) -> None:
    guild = _guild("Team 03", "Staff")
    at_sync: dict[str, object] = {}

    async def record_sync(synced_guild: Any) -> dict[str, object]:
        # The sync only removes team roles it still knows about, and only from members no longer linked
        at_sync["team_role"] = (await Team.objects.aget(pk=competition.pk)).discord_role_id
        at_sync["team_linked"] = await DiscordLink.objects.filter(discord_id=1, is_active=True).aexists()
        return {"roles_added": 0, "roles_removed": 4, "errors": 0, "changes": []}

    with (
        patch("bot.competition_actions.log_to_ops_channel", new_callable=AsyncMock),
        patch("bot.competition_actions.update_status_channel", new_callable=AsyncMock),
        patch("bot.role_sync.sync_roles", side_effect=record_sync) as sync,
    ):
        await run_competition_cleanup(MagicMock(), guild, "web:admin")

    sync.assert_awaited_once_with(guild)
    assert at_sync == {"team_role": 11, "team_linked": False}

    guild.categories[0].delete.assert_awaited_once()
    guild.categories[1].delete.assert_not_awaited()
    assert not await DiscordLink.objects.filter(discord_id=1, is_active=True).aexists()
    assert await DiscordLink.objects.filter(discord_id=2, is_active=True).aexists()  # staff link kept
    await competition.arefresh_from_db()
    assert (competition.discord_role_id, competition.discord_category_id) == (None, None)
    config = await CompetitionConfig.objects.aget(pk=1)
    assert (config.competition_start_time, config.start_message) == (None, "")
    assert not await QuotientMetadataCache.objects.aexists()
    assert (await AuditLog.objects.aget(action="competition_cleanup")).admin_user == "web:admin"


async def test_refuses_while_the_competition_runs(competition: Team) -> None:
    await CompetitionConfig.objects.filter(pk=1).aupdate(applications_enabled=True)
    with patch("bot.competition_actions.log_to_ops_channel", new_callable=AsyncMock) as ops:
        await run_competition_cleanup(MagicMock(), _guild("Team 03"), "web:admin")

    assert "still running" in ops.await_args.args[1]
    assert await DiscordLink.objects.filter(discord_id=1, is_active=True).aexists()


async def test_queued_cleanup_runs_the_shared_function() -> None:
    task = await DiscordTask.objects.acreate(task_type="cleanup_competition", payload={"requested_by": "web:admin"})
    processor = DiscordQueueProcessor(MagicMock())
    guild = MagicMock()
    processor._guild = MagicMock(return_value=guild)
    with patch("bot.competition_actions.run_competition_cleanup", new_callable=AsyncMock) as cleanup:
        await processor._handle_cleanup_competition(task.typed_payload())

    cleanup.assert_awaited_once_with(processor.bot, guild, "web:admin")
