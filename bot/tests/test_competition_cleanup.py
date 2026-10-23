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
from core.models import AuditLog, CompetitionConfig, DiscordTask, QueuedAnnouncement
from team.models import DiscordLink, Team

pytestmark = [pytest.mark.asyncio, pytest.mark.django_db(transaction=True)]


def _guild(*category_names: str) -> MagicMock:
    guild = MagicMock(spec=discord.Guild)
    categories = []
    for idx, name in enumerate(category_names):
        category = MagicMock(spec=discord.CategoryChannel)
        category.id = 22 if idx == 0 else 100 + idx
        category.name = name
        category.channels = [AsyncMock(spec=discord.TextChannel)]
        category.delete = AsyncMock()
        categories.append(category)
    guild.categories = categories
    by_id = {c.id: c for c in categories}
    guild.get_channel.side_effect = by_id.get
    role = MagicMock(spec=discord.Role)
    role.id = 11
    role.name = "Team 03"
    role.delete = AsyncMock()
    guild.roles = [role]
    guild.get_role.side_effect = lambda role_id: role if role_id == 11 else None
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


async def test_tears_down_discord_by_name_when_db_ids_are_null(competition: Team) -> None:
    await Team.objects.filter(pk=competition.pk).aupdate(discord_role_id=None, discord_category_id=None)
    guild = _guild("team 03", "Staff")

    with (
        patch("bot.competition_actions.log_to_ops_channel", new_callable=AsyncMock),
        patch("bot.competition_actions.update_status_channel", new_callable=AsyncMock),
        patch("bot.role_sync.sync_roles", _sync_removing(0)),
    ):
        await run_competition_cleanup(MagicMock(), guild, "web:admin")

    guild.categories[0].delete.assert_awaited_once()
    guild.roles[0].delete.assert_awaited_once()


async def test_refuses_while_the_competition_runs(competition: Team) -> None:
    await CompetitionConfig.objects.filter(pk=1).aupdate(applications_enabled=True)
    with patch("bot.competition_actions.log_to_ops_channel", new_callable=AsyncMock) as ops:
        await run_competition_cleanup(MagicMock(), _guild("Team 03"), "web:admin")

    assert "still running" in ops.await_args.args[1]
    assert await DiscordLink.objects.filter(discord_id=1, is_active=True).aexists()


@pytest.mark.parametrize(
    ("payload", "closed_out"),
    [({"requested_by": "web:admin", "closed_out_event_id": 5}, 5), ({"requested_by": "web:admin"}, None)],
    ids=["close-out", "queued-before-the-field"],
)
async def test_queued_cleanup_runs_the_shared_function(payload: dict[str, object], closed_out: int | None) -> None:
    """A row queued before closed_out_event_id existed still parses, and runs the full cleanup as it did then."""
    task = await DiscordTask.objects.acreate(task_type="cleanup_competition", payload=payload)
    processor = DiscordQueueProcessor(MagicMock())
    guild = MagicMock()
    processor._guild = MagicMock(return_value=guild)
    with patch("bot.competition_actions.run_competition_cleanup", new_callable=AsyncMock) as cleanup:
        await processor._handle_cleanup_competition(task.typed_payload())

    cleanup.assert_awaited_once_with(processor.bot, guild, "web:admin", closed_out_event_id=closed_out)


def _sync_removing(count: int) -> AsyncMock:
    return AsyncMock(return_value={"roles_added": 0, "roles_removed": count, "errors": 0, "changes": []})


async def test_after_a_close_out_it_does_only_the_discord_side(competition: Team) -> None:
    """The close-out reset the database; a schedule and start message set since belong to the next event."""
    from registration.models import Event, Season

    season = await Season.objects.acreate(name="2026", year=2026)
    event = await Event.objects.acreate(season=season, name="Inv", event_type="invitational", date="2026-10-03")
    await QueuedAnnouncement.objects.acreate(team=competition, message="Next event", sender_name="gold")
    guild = _guild("Team 03")

    with (
        patch("bot.competition_actions.log_to_ops_channel", new_callable=AsyncMock) as ops,
        patch("bot.competition_actions.update_status_channel", new_callable=AsyncMock),
        patch("bot.role_sync.sync_roles", _sync_removing(4)),
    ):
        await run_competition_cleanup(MagicMock(), guild, "web:admin", closed_out_event_id=event.pk)

    guild.categories[0].delete.assert_awaited_once()
    await competition.arefresh_from_db()
    assert (competition.discord_role_id, competition.discord_category_id) == (None, None)
    config = await CompetitionConfig.objects.aget(pk=1)
    assert config.competition_start_time is not None
    assert config.start_message == "Default VM login"
    assert await QuotientMetadataCache.objects.aexists()
    assert await QueuedAnnouncement.objects.aexists()
    assert not await AuditLog.objects.filter(action="user_unlinked").aexists()
    messages = [call.args[1] for call in ops.await_args_list]
    assert not any("Deactivated" in message for message in messages)


async def test_after_a_close_out_it_skips_once_another_event_opened(competition: Team) -> None:
    from registration.models import Event, Season

    season = await Season.objects.acreate(name="2026", year=2026)
    closed = await Event.objects.acreate(season=season, name="Inv", event_type="invitational", date="2026-10-03")
    await Event.objects.acreate(season=season, name="Qual", event_type="qualifier", date="2026-11-07", is_active=True)
    guild = _guild("Team 03")

    with (
        patch("bot.competition_actions.log_to_ops_channel", new_callable=AsyncMock) as ops,
        patch("bot.role_sync.sync_roles", _sync_removing(0)) as sync,
    ):
        await run_competition_cleanup(MagicMock(), guild, "web:admin", closed_out_event_id=closed.pk)

    assert "skipped: Qual (2026) was opened since" in ops.await_args.args[1]
    guild.categories[0].delete.assert_not_awaited()
    sync.assert_not_awaited()
    assert (await Team.objects.aget(pk=competition.pk)).discord_role_id == 11
