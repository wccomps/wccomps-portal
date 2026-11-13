"""run_competition: the one implementation behind the web page, the slash commands and the timer."""

import threading
from collections.abc import Iterator
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone

from core.authentik_manager import AuthentikAPIError
from core.discord_tasks import BroadcastMessage
from core.models import AuditLog, CompetitionConfig, DiscordTask
from core.services.competition import (
    COMPETITION_RUN_LOCK_ID,
    RUN_IN_PROGRESS,
    RUN_SUPERSEDED,
    STEPPED_ACROSS_THREADS,
    Attempt,
    CompetitionRunResult,
    RunOutcome,
    current_timer_plan,
    run_competition,
)
from core.utils import Stepped, drain
from team.models import MAX_TEAMS, Team, team_username

pytestmark = pytest.mark.django_db


def run_competition_to_completion(enable: bool, actor: str) -> CompetitionRunResult:
    return drain(run_competition(enable, actor))


@pytest.fixture(autouse=True)
def _team_accounts_through_the_manager():
    def switch(manager: MagicMock, number: int, is_active: bool) -> None:
        ok, why = manager.team_account(team_username(number), is_active=is_active)
        if not ok:
            raise AuthentikAPIError(why)

    with patch("core.services.competition.set_team_account_active", side_effect=switch):
        yield


@pytest.fixture
def authentik() -> Iterator[MagicMock]:
    manager = MagicMock()
    manager.__enter__.return_value = manager
    manager.team_account.return_value = (True, "")
    with (
        patch("core.authentik_manager.AuthentikManager", return_value=manager),
        patch("core.services.user_groups.refresh_user_groups") as refresh,
        patch("scoring.quotient_sync.sync_quotient_metadata") as sync_metadata,
        patch(
            "scoring.quotient_sync.sync_service_scores", return_value={"total": 0, "service_max_note": None}
        ) as sync_scores,
    ):
        manager.refresh = refresh
        manager.sync_metadata = sync_metadata
        manager.sync_scores = sync_scores
        yield manager


ACTIVE_TEAMS = [1, 2, 3]


@pytest.fixture(autouse=True)
def teams() -> None:
    """Three teams competing, two left over from an earlier event."""
    for number in range(1, 6):
        Team.objects.create(team_number=number, team_name=f"Team {number}", is_active=number in ACTIVE_TEAMS)


@pytest.fixture
def config() -> CompetitionConfig:
    now = timezone.now()
    config, _ = CompetitionConfig.objects.update_or_create(
        pk=1,
        defaults={
            "controlled_applications": ["scoring", "netbird"],
            "applications_enabled": False,
            "competition_start_time": now,
            "competition_end_time": now + timedelta(hours=8),
        },
    )
    return config


def test_start_enables_apps_and_accounts_refreshes_groups_and_audits(authentik, config):
    result = run_competition_to_completion(True, "timer")

    assert result.outcome is RunOutcome.COMPLETE
    assert result.apps_ok == ["scoring", "netbird"]
    assert result.accounts_ok == result.accounts_total == len(ACTIVE_TEAMS)
    authentik.refresh.assert_called_once()
    authentik.sync_metadata.assert_called_once_with(None)
    authentik.sync_scores.assert_not_called()
    config.refresh_from_db()
    assert config.applications_enabled
    assert config.competition_start_time is None
    assert config.competition_end_time is not None
    assert AuditLog.objects.get(action="competition_started").admin_user == "timer"


def test_start_enables_only_the_active_teams_accounts(authentik, config):
    """Inactive teams' accounts keep their groups between events; enabling them would let them in."""
    run_competition_to_completion(True, "timer")

    enabled = [c.args[0] for c in authentik.team_account.call_args_list]
    assert enabled == ["team01", "team02", "team03"]
    assert all(c.kwargs == {"is_active": True} for c in authentik.team_account.call_args_list)


def test_stop_disables_every_team_account(authentik, config):
    """Including inactive teams' accounts, so one left enabled is closed too."""
    CompetitionConfig.objects.filter(pk=1).update(applications_enabled=True)

    result = run_competition_to_completion(False, "timer")

    assert authentik.team_account.call_count == result.accounts_total == MAX_TEAMS
    assert "Accounts disabled: 50/50" in result.summary()


def test_stop_syncs_service_scores_and_records_actor_user(authentik, config, django_user_model):
    user = django_user_model.objects.create_user(username="gold_admin")
    CompetitionConfig.objects.filter(pk=1).update(applications_enabled=True)

    steps = list(run_competition(False, "gold_admin"))
    result = run_competition_to_completion(False, "gold_admin")

    assert steps[-1].message == "Quotient service scores synced"
    assert steps[-1].ok is True
    authentik.sync_scores.assert_called_with(user)
    authentik.sync_metadata.assert_not_called()
    assert result.quotient_synced is True
    assert "Quotient service scores: synced" in result.summary()
    audit = AuditLog.objects.filter(action="competition_stopped").order_by("-pk").first()
    assert audit is not None and audit.details["quotient_synced"] is True


def test_stop_keeps_the_note_that_the_service_maximum_was_not_updated(authentik, config):
    """In the step, and in what outlasts it: the summary the bot posts (ahead of the per-account failure lines, which
    the post's length limit cuts first) and the audit entry."""
    CompetitionConfig.objects.filter(pk=1).update(applications_enabled=True)
    note = "The service maximum was not updated: Quotient's export gave no max points."
    authentik.sync_scores.return_value = {"total": 3, "service_max_note": note}
    authentik.team_account.return_value = (False, "timed out")

    steps = []
    gen = run_competition(False, "gold_admin")
    while True:
        try:
            steps.append(next(gen))
        except StopIteration as done:
            result = done.value
            break

    assert (steps[-1].message, steps[-1].ok) == (f"Quotient service scores synced. {note}", False)
    assert result.summary().index(note) < result.summary().index("Failed: team")
    assert AuditLog.objects.get(action="competition_stopped").details["service_max_note"] == note


def test_stop_records_failed_score_sync_without_aborting(authentik, config):
    CompetitionConfig.objects.filter(pk=1).update(applications_enabled=True)
    authentik.sync_scores.side_effect = ValueError("Failed to retrieve service scores from Quotient")

    steps = []
    gen = run_competition(False, "timer")
    while True:
        try:
            steps.append(next(gen))
        except StopIteration as done:
            result = done.value
            break

    assert steps[-1].message == "Quotient score sync failed: Failed to retrieve service scores from Quotient"
    assert steps[-1].ok is False
    assert result.outcome is RunOutcome.COMPLETE
    assert result.quotient_synced is False
    assert "Quotient service scores: sync failed" in result.summary()
    config.refresh_from_db()
    assert not config.applications_enabled
    assert AuditLog.objects.get(action="competition_stopped").details["quotient_synced"] is False


def test_stop_after_the_scheduled_stop_clears_it(authentik, config):
    CompetitionConfig.objects.filter(pk=1).update(
        applications_enabled=True, competition_start_time=None, competition_end_time=timezone.now()
    )

    result = run_competition_to_completion(False, "timer")

    assert result.outcome is not RunOutcome.ERRORED and authentik.set_application_enabled.call_count == 2
    config.refresh_from_db()
    assert not config.applications_enabled
    assert config.competition_end_time is None


def test_a_paused_competition_started_again_still_stops_on_schedule(authentik, config):
    end = timezone.now() + timedelta(hours=4)
    CompetitionConfig.objects.filter(pk=1).update(
        applications_enabled=True, competition_start_time=None, competition_end_time=end
    )

    run_competition_to_completion(False, "discord:admin")
    config.refresh_from_db()
    assert (config.applications_enabled, config.competition_end_time) == (False, end)

    run_competition_to_completion(True, "discord:admin")
    config.refresh_from_db()
    assert (config.applications_enabled, config.competition_end_time) == (True, end)


@pytest.mark.parametrize(
    ("start_ago", "end_in"),
    [(timedelta(hours=9), -timedelta(hours=1)), (timedelta(seconds=30), timedelta(hours=8))],
    ids=["after-the-window", "just-after-the-start-time"],
)
def test_a_stop_cancels_a_start_time_that_has_passed(authentik, config, start_ago, end_in):
    """A start time still set is one the timer hasn't run, even once it passed (the bot was down, or the stop came
    before the next tick). The stop wins: the start is cancelled, so the timer can't start it after the stop, and an
    end still to come stays for a restart."""
    now = timezone.now()
    CompetitionConfig.objects.filter(pk=1).update(
        applications_enabled=False, competition_start_time=now - start_ago, competition_end_time=now + end_in
    )

    run_competition_to_completion(False, "discord:admin")

    config.refresh_from_db()
    assert config.competition_start_time is None
    assert config.competition_end_time == (now + end_in if end_in > timedelta(0) else None)
    assert not config.should_enable_applications()


def test_a_missed_window_is_cleared():
    from core.services.competition import clear_missed_window

    now = timezone.now()
    CompetitionConfig.objects.update_or_create(
        pk=1,
        defaults={
            "applications_enabled": False,
            "competition_start_time": now - timedelta(hours=9),
            "competition_end_time": now - timedelta(hours=1),
        },
    )

    assert clear_missed_window("timer")

    config = CompetitionConfig.get_config()
    assert (config.competition_start_time, config.competition_end_time) == (None, None)
    assert AuditLog.objects.get(action="competition_schedule_set").admin_user == "timer"
    assert not clear_missed_window("timer")


@pytest.mark.parametrize(
    ("enabled", "end_in"),
    [(False, timedelta(hours=1)), (True, -timedelta(hours=1)), (False, None)],
    ids=["window-still-open", "running", "no-end"],
)
def test_a_window_that_can_still_fire_or_is_running_is_kept(enabled, end_in):
    from core.services.competition import clear_missed_window

    now = timezone.now()
    CompetitionConfig.objects.update_or_create(
        pk=1,
        defaults={
            "applications_enabled": enabled,
            "competition_start_time": now - timedelta(hours=2),
            "competition_end_time": now + end_in if end_in is not None else None,
        },
    )

    assert not clear_missed_window("timer")
    assert CompetitionConfig.get_config().competition_start_time == now - timedelta(hours=2)


def test_stop_before_a_scheduled_start_keeps_that_runs_stop(authentik, config):
    start = timezone.now() + timedelta(hours=1)
    CompetitionConfig.objects.filter(pk=1).update(
        competition_start_time=start, competition_end_time=start + timedelta(hours=8)
    )

    run_competition_to_completion(False, "discord:admin")

    config.refresh_from_db()
    assert (config.competition_start_time, config.competition_end_time) == (start, start + timedelta(hours=8))


def test_schedule_edited_during_a_run_survives(authentik, config):
    """The run takes a while; it must not write back a stale copy of the whole config."""
    new_end = timezone.now() + timedelta(days=1)

    def edit_schedule_midway(username: str, is_active: bool) -> tuple[bool, str]:
        CompetitionConfig.objects.filter(pk=1).update(competition_end_time=new_end, max_team_members=7)
        return True, ""

    authentik.team_account.side_effect = edit_schedule_midway
    run_competition_to_completion(True, "timer")

    config.refresh_from_db()
    assert (config.competition_end_time, config.max_team_members) == (new_end, 7)


@pytest.mark.parametrize(("enable", "expected_accounts"), [(True, len(ACTIVE_TEAMS)), (False, MAX_TEAMS)])
def test_progress_counts_every_step(authentik, config, enable, expected_accounts):
    steps = list(run_competition(enable, "web:admin"))
    total = 2 + expected_accounts + 2  # apps, accounts, group refresh, Quotient sync
    assert [s.current for s in steps] == list(range(1, total + 1))
    assert {s.total for s in steps} == {total}


def test_no_controlled_apps_changes_nothing(authentik, config):
    CompetitionConfig.objects.filter(pk=1).update(controlled_applications=[])

    result = run_competition_to_completion(True, "timer")

    assert result.error == "No controlled applications configured"
    authentik.team_account.assert_not_called()
    assert not AuditLog.objects.exists()


def _queued_broadcasts() -> list[BroadcastMessage]:
    return [
        payload for task in DiscordTask.objects.all() if isinstance(payload := task.typed_payload(), BroadcastMessage)
    ]


def test_start_queues_the_start_message_to_every_active_team(authentik, config):
    CompetitionConfig.objects.filter(pk=1).update(start_message="Default login: admin / changeme")

    result = run_competition_to_completion(True, "timer")

    assert _queued_broadcasts() == [
        BroadcastMessage(target="all-teams", message="Default login: admin / changeme", sender="Black Team")
    ]
    assert "Start message queued" in result.summary()
    assert AuditLog.objects.get(action="competition_started").details["start_message_queued"] is True


def test_start_message_goes_out_once_when_the_start_is_retried(authentik, config):
    """The timer reruns a start that partly failed; teams must not get the credentials again."""
    CompetitionConfig.objects.filter(pk=1).update(start_message="Default login: admin / changeme")

    run_competition_to_completion(True, "timer")
    retry = run_competition_to_completion(True, "timer")

    assert len(_queued_broadcasts()) == 1
    assert not retry.start_message_queued


def test_start_message_is_not_queued_again_after_stopping_and_restarting(authentik, config):
    """Stopping and starting again during the same event must not broadcast the start message a second time."""
    CompetitionConfig.objects.filter(pk=1).update(start_message="Default login: admin / changeme")

    first = run_competition_to_completion(True, "timer")
    run_competition_to_completion(False, "discord:admin")
    second = run_competition_to_completion(True, "discord:admin")

    assert first.start_message_queued
    assert not second.start_message_queued
    assert len(_queued_broadcasts()) == 1


def test_blank_start_message_sends_nothing(authentik, config):
    CompetitionConfig.objects.filter(pk=1).update(start_message="  \n")

    result = run_competition_to_completion(True, "timer")

    assert _queued_broadcasts() == []
    assert not result.start_message_queued


def test_stop_never_sends_the_start_message(authentik, config):
    CompetitionConfig.objects.filter(pk=1).update(applications_enabled=True, start_message="Default login")

    run_competition_to_completion(False, "timer")

    assert _queued_broadcasts() == []


def _run_state() -> tuple[object, ...]:
    config = CompetitionConfig.get_config()
    return (
        config.last_run_incomplete,
        config.last_start_accounts_ok,
        config.last_start_accounts_tried,
    )


def test_a_run_records_on_the_config_what_it_left_undone(authentik, config):
    authentik.team_account.side_effect = [(True, ""), (False, "timeout"), (True, "")]
    run_competition_to_completion(True, "timer")

    assert _run_state() == (True, 2, 3)

    authentik.team_account.side_effect = None
    run_competition_to_completion(False, "discord:admin")

    assert _run_state() == (False, 2, 3)


def _audit_run(action: str, details: dict[str, object]) -> None:
    AuditLog.objects.create(
        action=action, admin_user="g", target_entity="competition_config", target_id=1, details=details
    )


@pytest.mark.parametrize(
    ("runs", "expected"),
    [
        ([], (False, None, None)),
        ([("competition_started", {})], (False, None, None)),
        (
            [("competition_started", {"apps_failed": [], "accounts_ok": 33, "accounts_failed": 1})],
            (True, 33, 34),
        ),
        (
            [
                ("competition_started", {"apps_failed": [], "accounts_ok": 3, "accounts_failed": 0}),
                ("competition_stopped", {"apps_failed": ["scoring"], "accounts_ok": 50, "accounts_failed": 0}),
            ],
            (True, 3, 3),
        ),
    ],
    ids=["no-run", "start-before-counts", "incomplete-start", "incomplete-stop"],
)
def test_the_migration_takes_the_last_runs_from_the_audit_log(runs, expected):
    import importlib

    from django.apps import apps

    for action, details in runs:
        _audit_run(action, details)

    importlib.import_module("core.migrations.0022_competitionconfig_last_run").record_last_runs(apps, None)

    assert _run_state() == expected


def test_a_failed_account_step_says_why(authentik, config):
    authentik.team_account.return_value = (False, "User team02 not found")

    stepped = Stepped(run_competition(True, "timer"))
    steps = list(stepped)
    result = stepped.result

    assert any(s.message == "Failed team01: User team02 not found" for s in steps)
    assert ("team01", "User team02 not found") in result.account_failures
    assert "Failed: team01: User team02 not found" in result.summary()
    audit = AuditLog.objects.get(action="competition_started")
    assert audit.details["account_failures"]["team01"] == "User team02 not found"


def test_start_provisions_nextcloud_accounts_when_configured(authentik, config, settings):
    from core.nextcloud import NextcloudAPIError

    settings.NEXTCLOUD_URL = "https://files.test"
    settings.NEXTCLOUD_USERNAME = "admin"
    settings.NEXTCLOUD_PASSWORD = "secret"
    nc = MagicMock()
    nc.__enter__.return_value = nc
    nc.provision_team_user.side_effect = [None, NextcloudAPIError("Provision Nextcloud user team02: HTTP 502"), None]

    with patch("core.nextcloud.NextcloudManager", return_value=nc):
        result = run_competition_to_completion(True, "timer")

    assert [c.args[0] for c in nc.provision_team_user.call_args_list] == ACTIVE_TEAMS
    assert result.outcome is RunOutcome.INCOMPLETE
    assert result.nextcloud_ok == 2
    assert result.nextcloud_failed == [("team02", "Provision Nextcloud user team02: HTTP 502")]
    assert "Nextcloud accounts provisioned: 2/3" in result.summary()
    assert "Failed Nextcloud: team02: Provision Nextcloud user team02: HTTP 502" in result.summary()
    audit = AuditLog.objects.get(action="competition_started")
    assert audit.details["nextcloud_ok"] == 2
    assert audit.details["nextcloud_failed"] == {"team02": "Provision Nextcloud user team02: HTTP 502"}


def test_a_failed_group_refresh_never_shows_authentiks_url_or_answer(authentik, config):
    from core.authentik_manager import AuthentikAPIError

    authentik.refresh.side_effect = AuthentikAPIError(
        "List /api/v3/core/groups/: Forbidden", 403, response_text='{"detail": "secret"}', url="https://auth.internal/x"
    )

    steps = list(run_competition(True, "timer"))

    assert any(
        s.message == "Permission refresh failed (retried within 5 minutes): List /api/v3/core/groups/: Forbidden"
        for s in steps
    )


def test_a_due_schedule_starts_a_new_series_over_one_in_the_other_direction(config):
    now = timezone.now()
    CompetitionConfig.objects.filter(pk=1).update(
        timer_series_enable=False, timer_series_attempt=2, timer_series_started=now - timedelta(minutes=5)
    )

    assert current_timer_plan(now) == Attempt(True, 1, now, config.competition_start_time)


def test_a_due_schedule_whose_attempt_errored_continues_its_series(config):
    """An attempt that errors leaves the schedule set: it must count on, not start over every minute."""
    now = timezone.now()
    started = now - timedelta(minutes=1)
    CompetitionConfig.objects.filter(pk=1).update(
        timer_series_enable=True, timer_series_attempt=1, timer_series_started=started
    )

    assert current_timer_plan(now) == Attempt(True, 2, started, None)


def _hold_run_lock(held: threading.Event, release: threading.Event) -> None:
    """Hold the run lock on a connection of this thread's own, the way a run in another process holds it."""
    from django.db import connection

    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_lock(%s)", [COMPETITION_RUN_LOCK_ID])
        held.set()
        release.wait(10)
    finally:
        connection.close()


@pytest.fixture
def run_lock_held_elsewhere() -> Iterator[None]:
    held, release = threading.Event(), threading.Event()
    holder = threading.Thread(target=_hold_run_lock, args=(held, release))
    holder.start()
    held.wait(10)
    yield
    release.set()
    holder.join(10)


@pytest.mark.django_db(transaction=True)
def test_a_stop_pressed_while_a_timer_attempt_runs_is_refused_not_undone(authentik, config, run_lock_held_elsewhere):
    result = run_competition_to_completion(False, "gold_admin")

    assert (result.error, result.outcome) == (RUN_IN_PROGRESS, RunOutcome.REFUSED)
    authentik.team_account.assert_not_called()
    assert not AuditLog.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_a_stop_made_between_timer_attempts_ends_the_retries(authentik, config):
    """The timer planned its next attempt before the stop; under the run lock it sees the stop and does nothing."""
    authentik.team_account.return_value = (False, "timed out")
    first = current_timer_plan(timezone.now())
    assert isinstance(first, Attempt)
    drain(run_competition(True, "timer", first))
    retry = current_timer_plan(timezone.now())
    assert retry == Attempt(True, 2, first.first_started, first.schedule)
    authentik.team_account.return_value = (True, "")
    run_competition_to_completion(False, "gold_admin")
    authentik.team_account.reset_mock()

    retried = drain(run_competition(True, "timer", retry))

    assert (retried.error, retried.outcome) == (RUN_SUPERSEDED, RunOutcome.REFUSED)
    authentik.team_account.assert_not_called()
    assert not CompetitionConfig.get_config().applications_enabled
    assert current_timer_plan(timezone.now()) is None


@pytest.mark.django_db(transaction=True)
def test_a_due_attempt_whose_schedule_was_cleared_since_it_was_planned_is_refused(authentik, config):
    """A close out between the timer's plan and its run lock clears the schedule; the start must not go ahead."""
    planned = current_timer_plan(timezone.now())
    assert isinstance(planned, Attempt)
    CompetitionConfig.objects.filter(pk=1).update(competition_start_time=None)

    result = drain(run_competition(True, "timer", planned))

    assert result.outcome is RunOutcome.REFUSED
    authentik.set_application_enabled.assert_not_called()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("fails", ["errors", "raises"])
def test_a_timer_attempt_that_fails_advances_its_series_under_the_lock(authentik, config, fails):
    planned = current_timer_plan(timezone.now())
    if fails == "errors":
        CompetitionConfig.objects.filter(pk=1).update(controlled_applications=[])
        result = drain(run_competition(True, "timer", planned))
        assert result.outcome is RunOutcome.ERRORED
    else:
        authentik.set_application_enabled.side_effect = RuntimeError("Authentik went away")
        with pytest.raises(RuntimeError):
            drain(run_competition(True, "timer", planned))

    config = CompetitionConfig.get_config()
    assert (config.timer_series_enable, config.timer_series_attempt) == (True, 1)


def _series() -> tuple[bool | None, int]:
    config = CompetitionConfig.get_config()
    return config.timer_series_enable, config.timer_series_attempt


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("fails", ["incomplete", "raises"])
def test_a_series_an_admin_ends_during_a_timer_retry_stays_ended(authentik, config, fails):
    """Setting a new start time ends the series first; the attempt finishing must not bring it back."""
    from core.services.competition import end_timer_series

    started = timezone.now() - timedelta(minutes=2)
    CompetitionConfig.objects.filter(pk=1).update(
        competition_start_time=None,
        timer_series_enable=True,
        timer_series_attempt=1,
        timer_series_started=started,
        timer_series_schedule=config.competition_start_time,
    )
    retry = current_timer_plan(timezone.now())
    assert retry == Attempt(True, 2, started, config.competition_start_time)

    def ended_meanwhile(*_args, **_kwargs):
        end_timer_series(True)
        if fails == "raises":
            raise RuntimeError("Authentik went away")
        raise AuthentikAPIError("timed out")

    authentik.set_application_enabled.side_effect = ended_meanwhile
    if fails == "raises":
        with pytest.raises(RuntimeError):
            drain(run_competition(True, "timer", retry))
    else:
        assert drain(run_competition(True, "timer", retry)).outcome is RunOutcome.INCOMPLETE

    assert _series() == (None, 0)


@pytest.mark.django_db(transaction=True)
def test_a_first_attempt_rescheduled_while_it_runs_starts_no_series(authentik, config):
    """No series exists yet to end; the new start time alone must stop the retries."""
    planned = current_timer_plan(timezone.now())
    later = timezone.now() + timedelta(hours=2)

    def rescheduled_meanwhile(*_args, **_kwargs):
        CompetitionConfig.objects.filter(pk=1).update(competition_start_time=later)
        raise AuthentikAPIError("timed out")

    authentik.set_application_enabled.side_effect = rescheduled_meanwhile
    assert drain(run_competition(True, "timer", planned)).outcome is RunOutcome.INCOMPLETE

    assert _series() == (None, 0)
    assert CompetitionConfig.get_config().competition_start_time == later


@pytest.mark.django_db(transaction=True)
def test_a_timer_retry_that_finishes_incomplete_counts_on(authentik, config):
    started = timezone.now() - timedelta(minutes=2)
    CompetitionConfig.objects.filter(pk=1).update(
        competition_start_time=None, timer_series_enable=True, timer_series_attempt=1, timer_series_started=started
    )
    authentik.team_account.return_value = (False, "timed out")

    drain(run_competition(True, "timer", current_timer_plan(timezone.now())))

    assert _series() == (True, 2)


@pytest.mark.django_db(transaction=True)
def test_a_timer_attempt_finishing_while_a_new_time_is_saved_starts_no_series(authentik, config):
    """Its final write waits for the new time to commit, then finds the schedule it carried out replaced."""
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import suppress

    from django.db import connection

    from core.services import competition

    planned = current_timer_plan(timezone.now())
    authentik.team_account.return_value = (False, "timed out")
    later = timezone.now() + timedelta(hours=2)
    real_end = competition.end_timer_series
    attempts = []

    def attempt() -> RunOutcome:
        try:
            return drain(run_competition(True, "timer", planned)).outcome
        finally:
            connection.close()

    def end_then_let_the_attempt_finish(enable: bool) -> None:
        real_end(enable)
        attempts.append(pool.submit(attempt))
        with suppress(TimeoutError):
            attempts[-1].result(timeout=3)

    with (
        ThreadPoolExecutor(1) as pool,
        patch.object(competition, "end_timer_series", end_then_let_the_attempt_finish),
    ):
        competition.set_schedule(start=later)
        assert attempts[0].result(timeout=30) is RunOutcome.INCOMPLETE

    assert _series() == (None, 0)
    assert CompetitionConfig.get_config().competition_start_time == later


@pytest.mark.django_db(transaction=True)
def test_a_first_attempt_whose_schedule_was_cleared_while_it_ran_starts_no_series(authentik, config):
    """A cleanup clears the schedule without the run lock; the attempt must not leave the timer a series to retry."""
    planned = current_timer_plan(timezone.now())

    def cleared_meanwhile(*_args, **_kwargs):
        CompetitionConfig.objects.filter(pk=1).update(competition_start_time=None)
        raise AuthentikAPIError("timed out")

    authentik.set_application_enabled.side_effect = cleared_meanwhile
    assert drain(run_competition(True, "timer", planned)).outcome is RunOutcome.INCOMPLETE

    assert _series() == (None, 0)


def _run_lock_is_free() -> bool:
    from django.db import connection

    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_lock(%s)", [COMPETITION_RUN_LOCK_ID])
            return cursor.fetchone()[0]
    finally:
        connection.close()


@pytest.mark.django_db(transaction=True)
def test_a_run_stepped_from_two_threads_is_stopped_and_lets_the_lock_go(authentik, config):
    """The run lock is the stepping thread's connection's: a step on another thread would run without it."""
    from concurrent.futures import ThreadPoolExecutor

    steps = run_competition(True, "gold_admin")
    next(steps)
    with ThreadPoolExecutor(1) as other, pytest.raises(RuntimeError, match=STEPPED_ACROSS_THREADS):
        other.submit(next, steps).result()
    with ThreadPoolExecutor(1) as elsewhere:
        assert elsewhere.submit(_run_lock_is_free).result()


@pytest.mark.django_db(transaction=True)
def test_a_timer_retry_keeps_a_schedule_an_admin_set_since_the_series_began(authentik, config):
    """The retry clears only the scheduled time its series carries out."""
    authentik.team_account.return_value = (False, "timed out")
    first = current_timer_plan(timezone.now())
    drain(run_competition(True, "timer", first))
    retry = current_timer_plan(timezone.now())
    later = timezone.now() + timedelta(hours=2)
    CompetitionConfig.objects.filter(pk=1).update(competition_start_time=later)

    drain(run_competition(True, "timer", retry))

    assert CompetitionConfig.get_config().competition_start_time == later


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("rescheduled", [False, True], ids=["unchanged", "rescheduled"])
def test_giving_up_drops_only_the_schedule_its_series_carried_out(config, rescheduled):
    from core.services.competition import GiveUp, give_up_timer_series

    original = config.competition_start_time
    started = timezone.now() - timedelta(minutes=5)
    CompetitionConfig.objects.filter(pk=1).update(
        timer_series_enable=True, timer_series_attempt=4, timer_series_started=started, timer_series_schedule=original
    )
    later = timezone.now() + timedelta(hours=2)
    if rescheduled:
        CompetitionConfig.objects.filter(pk=1).update(competition_start_time=later)

    assert give_up_timer_series(GiveUp(True, 4, started, original, "still incomplete after 4 attempts"))

    config = CompetitionConfig.get_config()
    assert config.timer_series_enable is None
    assert config.competition_start_time == (later if rescheduled else None)


def test_setting_a_schedule_ends_a_live_series_for_its_direction_only(config):
    from core.services.competition import end_timer_series

    started = timezone.now()
    CompetitionConfig.objects.filter(pk=1).update(
        timer_series_enable=False, timer_series_attempt=2, timer_series_started=started
    )

    end_timer_series(True)
    assert CompetitionConfig.get_config().timer_series_enable is False
    end_timer_series(False)
    assert CompetitionConfig.get_config().timer_series_enable is None


def test_an_end_left_by_a_stop_is_cleared_once_it_passes(authentik, config):
    """A stop keeps an end still to come. Once it passes with the competition stopped, it is cleared, so it doesn't
    refuse a new start time (a start clears it too)."""
    from core.services.competition import clear_missed_window, set_schedule

    end = timezone.now() + timedelta(hours=1)
    CompetitionConfig.objects.filter(pk=1).update(
        applications_enabled=True, competition_start_time=None, competition_end_time=end
    )
    run_competition_to_completion(False, "discord:admin")
    CompetitionConfig.objects.filter(pk=1).update(competition_end_time=timezone.now() - timedelta(minutes=1))

    assert clear_missed_window("timer")
    set_schedule("discord:admin", start=timezone.now() + timedelta(days=1))

    config.refresh_from_db()
    assert config.competition_end_time is None


def test_a_start_clears_an_end_that_has_passed(authentik, config):
    CompetitionConfig.objects.filter(pk=1).update(
        applications_enabled=False, competition_start_time=None, competition_end_time=timezone.now()
    )

    run_competition_to_completion(True, "discord:admin")

    config.refresh_from_db()
    assert (config.applications_enabled, config.competition_end_time) == (True, None)
    assert not config.should_disable_applications()


def test_a_start_keeps_a_start_time_set_while_it_ran(authentik, config):
    """The run does its Authentik work before taking the lock: a new start set meanwhile is not the one it used up."""
    new_start = timezone.now() + timedelta(days=1)

    def set_a_start_meanwhile(username: str, is_active: bool) -> tuple[bool, str]:
        CompetitionConfig.objects.filter(pk=1).update(competition_start_time=new_start)
        return True, ""

    authentik.team_account.side_effect = set_a_start_meanwhile
    run_competition_to_completion(True, "timer")

    config.refresh_from_db()
    assert (config.applications_enabled, config.competition_start_time) == (True, new_start)


def test_a_start_records_the_broadcast_it_queued(authentik, config):
    """So the reset for the next event can scrub that copy of the credentials, whatever the message says by then."""
    CompetitionConfig.objects.filter(pk=1).update(start_message="Default login")

    run_competition_to_completion(True, "timer")

    config.refresh_from_db()
    assert config.start_broadcasts == [DiscordTask.objects.get(task_type="broadcast_message").pk]


def test_a_start_marks_its_window_until_a_new_start_time_is_set(authentik, config):
    from core.services.competition import set_schedule

    run_competition_to_completion(True, "timer")
    assert CompetitionConfig.objects.get(pk=1).started_this_window
    run_competition_to_completion(False, "timer")
    assert CompetitionConfig.objects.get(pk=1).started_this_window

    set_schedule("gold", start=timezone.now() + timedelta(hours=1))
    assert not CompetitionConfig.objects.get(pk=1).started_this_window


def test_a_stop_with_no_end_set_ends_the_window(authentik, config):
    """So an end-only schedule set later reads as missed, not as after a manual stop."""
    CompetitionConfig.objects.filter(pk=1).update(competition_end_time=None)
    run_competition_to_completion(True, "timer")
    run_competition_to_completion(False, "gold")

    assert not CompetitionConfig.objects.get(pk=1).started_this_window
