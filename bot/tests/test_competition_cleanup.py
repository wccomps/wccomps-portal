"""The cleanups: the full one behind the slash command, Close Out's Discord teardown, and the teardown when an event
opens."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from django.contrib.auth.models import User
from django.utils import timezone
from registration.services import CloseOutError, DiscordCleanupReset, reset_for_discord_cleanup
from scoring.models import QuotientMetadataCache

from bot.competition_actions import reconcile_teams
from bot.discord_queue import DiscordQueueProcessor
from bot.utils import run_blocking
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


_MANAGER: list[Any] = []


def _setup_offline_manager() -> Any:
    return _MANAGER[-1]


@pytest.fixture(autouse=True)
def _setup_offline() -> Any:
    """The reconcile sets the linked teams up again and syncs roles; without Discord, those are recorded calls."""
    with (
        patch("bot.competition_actions.DiscordManager") as manager,
        patch("bot.role_sync.sync_roles", _sync_removing(0)),
    ):
        manager.return_value.setup_team_infrastructure = AsyncMock(return_value=(MagicMock(), MagicMock()))
        _MANAGER.append(manager)
        yield manager


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


def _no_beat() -> None:
    pass


def _sync_removing(count: int) -> AsyncMock:
    return AsyncMock(return_value={"roles_added": 0, "roles_removed": count, "errors": 0, "changes": []})


async def _reset(actor: str = "web:admin") -> DiscordCleanupReset:
    return await run_blocking(reset_for_discord_cleanup, actor)


async def _open_event() -> int:
    from registration.models import Event, Season

    season = await Season.objects.acreate(name="2026", year=2026)
    event = await Event.objects.acreate(
        season=season, name="Qual", event_type="qualifier", date="2026-11-07", is_active=True
    )
    return event.pk


async def test_the_full_cleanup_resets_then_queues_the_discord_teardown(competition: Team) -> None:
    guild = _guild("Team 03", "Staff")
    at_sync: dict[str, object] = {}

    async def record_sync(synced_guild: Any, **_: Any) -> dict[str, object]:
        # The first sync only removes team roles it still knows about, and only from members no longer linked
        if at_sync:
            return {"roles_added": 0, "roles_removed": 0, "errors": 0, "changes": []}
        at_sync["team_role"] = (await Team.objects.aget(pk=competition.pk)).discord_role_id
        at_sync["team_linked"] = await DiscordLink.objects.filter(discord_id=1, is_active=True).aexists()
        return {"roles_added": 0, "roles_removed": 4, "errors": 0, "changes": []}

    reset = await _reset()
    task = await DiscordTask.objects.aget(task_type="reconcile_teams")
    assert task.payload == {"requested_by": "web:admin", "open_event_id": None}
    with (
        patch("bot.competition_actions.log_to_ops_channel", new_callable=AsyncMock),
        patch("bot.competition_actions.update_status_channel", new_callable=AsyncMock),
        patch("bot.role_sync.sync_roles", side_effect=record_sync) as sync,
    ):
        await reconcile_teams(MagicMock(), guild, "web:admin", None, _no_beat)

    assert reset.unlinked == 1
    assert [call.args for call in sync.await_args_list] == [(guild,), (guild,)]
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
        await reconcile_teams(MagicMock(), guild, "web:admin", None, _no_beat)

    guild.categories[0].delete.assert_awaited_once()
    guild.roles[0].delete.assert_awaited_once()


async def test_each_teardown_beats_once_per_team(competition: Team) -> None:
    """A teardown is minutes of Discord calls on the queue: it beats per team torn down and per team set up."""
    await Team.objects.acreate(team_number=4, team_name="Team 04")
    beat = MagicMock()

    with (
        patch("bot.competition_actions.log_to_ops_channel", new_callable=AsyncMock),
        patch("bot.competition_actions.update_status_channel", new_callable=AsyncMock),
        patch("bot.role_sync.sync_roles", _sync_removing(0)),
    ):
        await reconcile_teams(MagicMock(), _guild("Team 03"), "web:admin", None, beat)

    assert beat.call_count == 3


@pytest.mark.parametrize("event_open", [False, True], ids=["running", "event-open"])
async def test_the_full_reset_refuses_writing_nothing(competition: Team, event_open: bool) -> None:
    """The open event's schedule, start message and team links are Close Out's to reset, not this command's."""
    if event_open:
        await _open_event()
    else:
        await CompetitionConfig.objects.filter(pk=1).aupdate(applications_enabled=True)

    with pytest.raises(CloseOutError, match=r"Qual \(2026\) is open" if event_open else "The competition is running"):
        await _reset()

    assert await DiscordLink.objects.filter(discord_id=1, is_active=True).aexists()
    config = await CompetitionConfig.objects.aget(pk=1)
    assert (config.start_message, config.competition_start_time is not None) == ("Default VM login", True)
    assert await QuotientMetadataCache.objects.aexists()
    assert not await DiscordTask.objects.aexists()


async def test_the_full_teardown_skips_while_the_competition_runs(competition: Team) -> None:
    await CompetitionConfig.objects.filter(pk=1).aupdate(applications_enabled=True)
    guild = _guild("Team 03")
    with patch("bot.competition_actions.log_to_ops_channel", new_callable=AsyncMock) as ops:
        await reconcile_teams(MagicMock(), guild, "web:admin", None, _no_beat)

    assert "skipped: The competition is running." in ops.await_args.args[1]
    guild.categories[0].delete.assert_not_awaited()


async def test_the_close_out_teardown_skips_while_the_competition_runs(competition: Team) -> None:
    await CompetitionConfig.objects.filter(pk=1).aupdate(applications_enabled=True)
    guild = _guild("Team 03")
    with patch("bot.competition_actions.log_to_ops_channel", new_callable=AsyncMock) as ops:
        await reconcile_teams(MagicMock(), guild, "web:admin", None, _no_beat)

    assert "skipped: The competition is running." in ops.await_args.args[1]
    guild.categories[0].delete.assert_not_awaited()


async def test_after_the_reset_nothing_is_left_over_and_it_is_on_record(competition: Team) -> None:
    """The deactivated team links stay as history, not as live data: the next school list import isn't held up. The
    reset is audited even if its teardown is skipped."""
    from asgiref.sync import sync_to_async
    from registration.stage import event_stage

    await _reset("discord:admin")

    assert not (await sync_to_async(event_stage)()).live_data
    audit = await AuditLog.objects.aget(action="competition_cleanup_reset")
    assert (audit.admin_user, audit.details["unlinked"]) == ("discord:admin", 1)


async def test_after_the_cleanup_and_an_import_the_unlinked_teams_are_free(competition: Team) -> None:
    """A deactivated link is history: the team can take a school again (Add schools, registration)."""
    from datetime import date

    from asgiref.sync import sync_to_async
    from registration.models import Event, Season
    from registration.services import SchoolListRow, free_teams, import_school_list
    from registration.tests.shown import new_event

    await Season.objects.acreate(name="2026", year=2026, is_active=True)
    await Team.objects.acreate(team_number=4, team_name="Team 04")
    await _reset()
    with patch("bot.competition_actions.log_to_ops_channel", new_callable=AsyncMock):
        await reconcile_teams(MagicMock(), _guild("Team 03"), "web:admin", None, _no_beat)
    rows = [SchoolListRow(team_number=4, school_name="Example University", contact_email="c@example.edu")]

    def import_then_free() -> list[int]:
        import_school_list(new_event("Next", date(2026, 11, 7), "qualifier"), rows, "gold")
        return list(free_teams(Event.objects.get(name="Next")).values_list("team_number", flat=True))

    assert await sync_to_async(import_then_free)() == [3]


async def test_the_reset_unlinks_the_team_members_in_its_own_transaction(competition: Team) -> None:
    """The reset unlinks the team members (staff stay), auditing each. An incomplete stop stays: an app or account it
    left on in Authentik keeps Stop offered."""
    await CompetitionConfig.objects.filter(pk=1).aupdate(last_run_incomplete=True)

    reset = await _reset("discord:admin")

    assert (await CompetitionConfig.objects.aget(pk=1)).last_run_incomplete
    assert reset.unlinked == 1
    assert not await DiscordLink.objects.filter(discord_id=1, is_active=True).aexists()
    assert await DiscordLink.objects.filter(discord_id=2, is_active=True).aexists()
    audit = await AuditLog.objects.aget(action="user_unlinked")
    assert (audit.admin_user, audit.target_id, audit.details["reason"]) == ("discord:admin", 1, "competition_cleanup")


async def test_waiting_for_the_school_list_lock_leaves_the_bots_shared_thread_free(competition: Team) -> None:
    """Close Out or an import can hold the lock for a while; every other ORM call in the bot runs on the shared
    thread and must not wait behind the cleanup's reset."""
    import asyncio
    import threading

    from asgiref.sync import sync_to_async
    from django.db import connection
    from registration.locks import SCHOOL_LIST_LOCK_ID

    held, release = threading.Event(), threading.Event()

    def hold_the_lock() -> None:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_lock(%s)", [SCHOOL_LIST_LOCK_ID])
            held.set()
            release.wait(10)
            cursor.execute("SELECT pg_advisory_unlock(%s)", [SCHOOL_LIST_LOCK_ID])
        connection.close()

    holder = threading.Thread(target=hold_the_lock)
    holder.start()
    held.wait(10)
    reset = asyncio.create_task(_reset())
    await asyncio.sleep(0.5)
    try:
        assert await asyncio.wait_for(sync_to_async(lambda: Team.objects.count())(), 5) == 1
    finally:
        release.set()
        await reset
        holder.join()
    assert not await DiscordLink.objects.filter(discord_id=1, is_active=True).aexists()


async def test_the_command_refuses_while_an_event_is_open(competition: Team, mock_interaction: Any) -> None:
    from bot.cogs.admin_competition import AdminCompetitionCog

    await _open_event()
    cog = AdminCompetitionCog(MagicMock())

    await cog.admin_cleanup_competition.callback(cog, mock_interaction)

    message = mock_interaction.response.send_message.await_args
    assert message.args[0].startswith("Qual (2026) is open")
    assert "view" not in message.kwargs


async def test_the_command_resets_and_says_what_it_did(competition: Team, mock_interaction: Any) -> None:
    from bot.cogs.admin_competition import AdminCompetitionCog

    cog = AdminCompetitionCog(MagicMock())
    with (
        patch("bot.cogs.admin_competition.ConfirmView") as view,
        patch("bot.cogs.admin_competition.log_to_ops_channel", new_callable=AsyncMock) as ops,
    ):
        view.return_value.wait = AsyncMock()
        view.return_value.confirmed = True
        await cog.admin_cleanup_competition.callback(cog, mock_interaction)

    reply = mock_interaction.followup.send.await_args.args[0]
    assert reply.startswith("Deactivated 1 team member links")
    assert "teardown is queued" in reply
    assert "Deactivated 1 team member links" in ops.await_args.args[1]
    assert await DiscordTask.objects.filter(task_type="reconcile_teams").aexists()


@pytest.mark.parametrize(
    ("payload", "open_event_id"),
    [
        ({"requested_by": "gold"}, None),
        ({"requested_by": "gold", "open_event_id": 6}, 6),
    ],
    ids=["no-event-open", "event-opened"],
)
async def test_each_queued_cleanup_runs_its_job(payload: dict[str, object], open_event_id: int | None) -> None:
    task = await DiscordTask.objects.acreate(task_type="reconcile_teams", payload=payload)
    processor = DiscordQueueProcessor(MagicMock())
    guild = MagicMock()
    processor._guild = MagicMock(return_value=guild)
    with patch("bot.competition_actions.reconcile_teams", new_callable=AsyncMock) as run:
        await processor._dispatch(task.typed_payload())

    run.assert_awaited_once_with(processor.bot, guild, "gold", open_event_id, processor._beat)


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {"requested_by": "gold", "closed_out_event_id": 5},
            ("reconcile_teams", "pending", {"requested_by": "gold", "open_event_id": None}),
        ),
        ({"requested_by": "gold"}, ("cleanup_competition", "failed", {"requested_by": "gold"})),
    ],
    ids=["close-out-row", "full-cleanup-row"],
)
async def test_the_migration_moves_rows_queued_before_it(
    payload: dict[str, object], expected: tuple[str, str, dict[str, object]]
) -> None:
    """An old bot would read a new row as its full cleanup with a database reset, so the reconcile has a type of its
    own. A close-out's old row becomes one; an older full-cleanup row also asked for a reset, so it fails."""
    import importlib

    from asgiref.sync import sync_to_async
    from django.apps import apps

    task = await DiscordTask.objects.acreate(task_type="cleanup_competition", payload=payload)
    migration = importlib.import_module("core.migrations.0020_discordtask_cleanup_jobs")

    await sync_to_async(migration.move_queued_cleanups)(apps, None)

    await task.arefresh_from_db()
    assert (task.task_type, task.status, task.payload) == expected


async def test_after_a_close_out_it_does_only_the_discord_side(competition: Team) -> None:
    """The close-out reset the database; a schedule and start message set since belong to the next event."""
    from registration.models import Event, Season

    season = await Season.objects.acreate(name="2026", year=2026)
    await Event.objects.acreate(season=season, name="Inv", event_type="invitational", date="2026-10-03")
    await QueuedAnnouncement.objects.acreate(team=competition, message="Next event", sender_name="gold")
    guild = _guild("Team 03")

    with (
        patch("bot.competition_actions.log_to_ops_channel", new_callable=AsyncMock) as ops,
        patch("bot.competition_actions.update_status_channel", new_callable=AsyncMock),
        patch("bot.role_sync.sync_roles", _sync_removing(4)),
    ):
        await reconcile_teams(MagicMock(), guild, "web:admin", None, _no_beat)

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
    await Event.objects.acreate(season=season, name="Inv", event_type="invitational", date="2026-10-03")
    await Event.objects.acreate(season=season, name="Qual", event_type="qualifier", date="2026-11-07", is_active=True)
    guild = _guild("Team 03")

    with (
        patch("bot.competition_actions.log_to_ops_channel", new_callable=AsyncMock) as ops,
        patch("bot.role_sync.sync_roles", _sync_removing(0)),
    ):
        await reconcile_teams(MagicMock(), guild, "web:admin", None, _no_beat)

    assert "skipped: Qual (2026) was opened since, and its opening tears them down" in ops.await_args.args[1]
    guild.categories[0].delete.assert_not_awaited()
    assert (await Team.objects.aget(pk=competition.pk)).discord_role_id == 11


async def test_opening_an_event_tears_down_what_the_last_event_left(competition: Team) -> None:
    """With the close-out's teardown skipped, the opening removes the team channels, roles and Room Judge role, keeps
    the open event's state and sets the linked teams up again."""
    opened = await _open_event()
    guild = _guild("Team 03", "Staff")
    judge = MagicMock()
    judge.remove_roles = AsyncMock()
    judge_role = MagicMock(spec=discord.Role)
    judge_role.name = "WCComps Room Judge"
    judge_role.members = [judge]
    guild.roles.append(judge_role)

    with (
        patch("bot.competition_actions.log_to_ops_channel", new_callable=AsyncMock) as ops,
        patch("bot.competition_actions.update_status_channel", new_callable=AsyncMock) as status,
        patch("bot.role_sync.sync_roles", _sync_removing(0)) as sync,
        patch("bot.competition_actions.DiscordManager") as manager,
    ):
        manager.return_value.setup_team_infrastructure = AsyncMock(return_value=(MagicMock(), MagicMock()))
        await reconcile_teams(MagicMock(), guild, "gold", opened, _no_beat)

    guild.categories[0].delete.assert_awaited_once()
    guild.categories[1].delete.assert_not_awaited()
    guild.roles[0].delete.assert_awaited_once()
    manager.return_value.setup_team_infrastructure.assert_awaited_once_with(3)
    assert sync.await_args_list[-1].args == (guild,)
    judge.remove_roles.assert_awaited_once_with(judge_role, reason="Competition cleanup")
    status.assert_awaited_once()
    assert await DiscordLink.objects.filter(discord_id=1, is_active=True).aexists()
    config = await CompetitionConfig.objects.aget(pk=1)
    assert (config.start_message, config.competition_start_time is not None) == ("Default VM login", True)
    assert "Qual (2026) opened" in ops.await_args.args[1]


async def test_opening_an_event_with_nothing_left_over_does_nothing(db: Any) -> None:
    await Team.objects.acreate(team_number=3, team_name="Team 03")
    opened = await _open_event()
    guild = _guild("Staff")
    guild.roles = []
    guild.get_role.side_effect = lambda role_id: None

    with (
        patch("bot.competition_actions.log_to_ops_channel", new_callable=AsyncMock) as ops,
        patch("bot.role_sync.sync_roles", _sync_removing(0)),
    ):
        await reconcile_teams(MagicMock(), guild, "gold", opened, _no_beat)
        await reconcile_teams(MagicMock(), guild, "gold", opened, _no_beat)

    guild.categories[0].delete.assert_not_awaited()
    ops.assert_not_awaited()
    assert not await AuditLog.objects.filter(action="competition_cleanup").aexists()


async def test_a_rerun_sets_up_the_linked_teams_even_with_nothing_left_to_remove(competition: Team) -> None:
    """A run cut off after the deletes left the linked teams without channels; the rerun finds nothing to delete."""
    opened = await _open_event()
    await Team.objects.filter(pk=competition.pk).aupdate(discord_role_id=None, discord_category_id=None)
    guild = _guild("Staff")
    guild.roles = []
    guild.get_role.side_effect = lambda role_id: None

    with (
        patch("bot.competition_actions.log_to_ops_channel", new_callable=AsyncMock),
        patch("bot.role_sync.sync_roles", _sync_removing(0)),
        patch("bot.competition_actions.DiscordManager") as manager,
    ):
        manager.return_value.setup_team_infrastructure = AsyncMock(return_value=(MagicMock(), MagicMock()))
        await reconcile_teams(MagicMock(), guild, "gold", opened, _no_beat)

    manager.return_value.setup_team_infrastructure.assert_awaited_once_with(3)


async def test_a_failing_opening_teardown_raises_so_the_queue_retries_it(competition: Team) -> None:
    opened = await _open_event()
    failing = AsyncMock(side_effect=discord.HTTPException(MagicMock(status=500), "x"))

    with (
        patch("bot.competition_actions.log_to_ops_channel", new_callable=AsyncMock),
        patch("bot.role_sync.sync_roles", _sync_removing(0)),
        patch("bot.competition_actions.delete_team_infrastructure", failing),
        pytest.raises(discord.HTTPException),
    ):
        await reconcile_teams(MagicMock(), _guild("Team 03"), "gold", opened, _no_beat)

    audit = await AuditLog.objects.aget(action="competition_cleanup")
    assert (audit.details["finished"], audit.details["deleted_categories"]) == (False, 0)
    _setup_offline_manager().return_value.setup_team_infrastructure.assert_awaited_once_with(3)


async def test_the_opening_teardown_stops_when_the_competition_starts_midway(competition: Team) -> None:
    """Each team's channels are a few Discord calls; a start can land between two teams. Every team with an active
    link is set up again: a running competition needs their channels most."""
    opened = await _open_event()
    await Team.objects.acreate(team_number=4, team_name="Team 04")
    deleted: list[int] = []

    async def delete_then_start(guild: Any, team: Team, reason: str) -> list[str]:
        deleted.append(team.team_number)
        await CompetitionConfig.objects.filter(pk=1).aupdate(applications_enabled=True)
        return ["category", "role"]

    with (
        patch("bot.competition_actions.log_to_ops_channel", new_callable=AsyncMock) as ops,
        patch("bot.competition_actions.update_status_channel", new_callable=AsyncMock),
        patch("bot.role_sync.sync_roles", _sync_removing(0)),
        patch("bot.competition_actions.delete_team_infrastructure", side_effect=delete_then_start),
        patch("bot.competition_actions.DiscordManager") as manager,
    ):
        manager.return_value.setup_team_infrastructure = AsyncMock(return_value=(MagicMock(), MagicMock()))
        await reconcile_teams(MagicMock(), _guild("Team 03"), "gold", opened, _no_beat)

    assert deleted == [3]
    manager.return_value.setup_team_infrastructure.assert_awaited_once_with(3)
    assert "Stopped before the end: The competition is running." in ops.await_args.args[1]


async def test_the_opening_teardown_never_touches_a_running_or_replaced_event(competition: Team) -> None:
    opened = await _open_event()
    await CompetitionConfig.objects.filter(pk=1).aupdate(applications_enabled=True)
    guild = _guild("Team 03")

    with patch("bot.competition_actions.log_to_ops_channel", new_callable=AsyncMock) as ops:
        await reconcile_teams(MagicMock(), guild, "gold", opened, _no_beat)
        await CompetitionConfig.objects.filter(pk=1).aupdate(applications_enabled=False)
        await reconcile_teams(MagicMock(), guild, "gold", opened + 1, _no_beat)

    guild.categories[0].delete.assert_not_awaited()
    messages = [call.args[1] for call in ops.await_args_list]
    assert "The competition is running" in messages[0]
    assert "no longer open" in messages[1]


async def test_a_team_whose_category_delete_failed_keeps_its_id_and_mark(competition: Team) -> None:
    """So no setup reuses the category and its role isn't granted, and the next hand-over queues its removal again."""
    from bot.discord_manager import delete_team_infrastructure

    guild = _guild("Team 03")
    guild.categories[0].delete = AsyncMock(side_effect=discord.HTTPException(MagicMock(status=500), "x"))

    await delete_team_infrastructure(guild, competition, reason="Competition cleanup")

    await competition.arefresh_from_db()
    assert (competition.discord_category_id, competition.discord_role_id) == (22, None)
    assert competition.channels_need_clearing


async def test_a_failing_teardown_reports_the_teams_left_before_it_raises(competition: Team) -> None:
    """Ops hear which teams kept their channels, not only the error after the last retry."""

    async def left(guild: Any, team: Team, reason: str) -> list[str]:
        team.channels_need_clearing = True
        return []

    setup = _setup_offline_manager().return_value.setup_team_infrastructure
    setup.side_effect = discord.HTTPException(MagicMock(status=500), "setup down")

    with (
        patch("bot.competition_actions.log_to_ops_channel", new_callable=AsyncMock) as ops,
        patch("bot.competition_actions.update_status_channel", new_callable=AsyncMock),
        patch("bot.role_sync.sync_roles", _sync_removing(0)),
        patch("bot.competition_actions.delete_team_infrastructure", side_effect=left),
        pytest.raises(discord.HTTPException),
    ):
        await reconcile_teams(MagicMock(), _guild("Team 03"), "gold", None, _no_beat)

    summary = ops.await_args.args[1]
    assert summary.startswith("Discord cleanup failed partway")
    assert "Could not delete the role or category of teams 03" in summary
