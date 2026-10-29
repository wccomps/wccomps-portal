"""run_competition: the one implementation behind the web page, the slash commands and the timer."""

import threading
from collections.abc import Iterator
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone

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
from core.utils import drain
from team.models import MAX_TEAMS, Team, team_username

pytestmark = pytest.mark.django_db


def run_competition_to_completion(enable: bool, actor: str) -> CompetitionRunResult:
    return drain(run_competition(enable, actor))


@pytest.fixture(autouse=True)
def _team_accounts_through_the_manager():
    with patch(
        "core.services.competition.set_team_account_active",
        side_effect=lambda manager, number, is_active: manager.team_account(team_username(number), is_active=is_active),
    ):
        yield


@pytest.fixture
def authentik() -> Iterator[MagicMock]:
    manager = MagicMock()
    manager.enable_application.return_value = (True, None)
    manager.disable_application.return_value = (True, None)
    manager.team_account.return_value = (True, "")
    with (
        patch("core.authentik_manager.AuthentikManager", return_value=manager),
        patch("core.services.user_groups.refresh_user_groups") as refresh,
        patch("scoring.quotient_sync.sync_quotient_metadata") as sync_metadata,
        patch("scoring.quotient_sync.sync_service_scores") as sync_scores,
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


def test_stop_clears_only_the_end_time(authentik, config):
    CompetitionConfig.objects.filter(pk=1).update(applications_enabled=True)

    result = run_competition_to_completion(False, "discord:admin")

    assert result.outcome is not RunOutcome.ERRORED and authentik.disable_application.call_count == 2
    config.refresh_from_db()
    assert not config.applications_enabled
    assert config.competition_end_time is None
    assert config.competition_start_time is not None


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


def test_blank_start_message_sends_nothing(authentik, config):
    CompetitionConfig.objects.filter(pk=1).update(start_message="  \n")

    result = run_competition_to_completion(True, "timer")

    assert _queued_broadcasts() == []
    assert not result.start_message_queued


def test_stop_never_sends_the_start_message(authentik, config):
    CompetitionConfig.objects.filter(pk=1).update(applications_enabled=True, start_message="Default login")

    run_competition_to_completion(False, "timer")

    assert _queued_broadcasts() == []


def test_the_last_start_reader_returns_the_accounts_or_none():
    from core.models import AuditLog
    from core.services.competition import COMPETITION_STARTED, last_start_accounts

    assert last_start_accounts() is None
    AuditLog.objects.create(
        action=COMPETITION_STARTED, admin_user="g", target_entity="competition_config", target_id=1, details={}
    )
    assert last_start_accounts() is None
    AuditLog.objects.create(
        action=COMPETITION_STARTED,
        admin_user="g",
        target_entity="competition_config",
        target_id=1,
        details={"accounts_ok": 2, "accounts_failed": 1},
    )
    assert last_start_accounts() == (2, 3)


def test_a_failed_account_step_says_why(authentik, config):
    authentik.team_account.return_value = (False, "User team02 not found")

    steps = list(run_competition(True, "timer"))

    assert any(s.message == "Failed team01: User team02 not found" for s in steps)


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
    authentik.enable_application.assert_not_called()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("fails", ["errors", "raises"])
def test_a_timer_attempt_that_fails_advances_its_series_under_the_lock(authentik, config, fails):
    planned = current_timer_plan(timezone.now())
    if fails == "errors":
        CompetitionConfig.objects.filter(pk=1).update(controlled_applications=[])
        result = drain(run_competition(True, "timer", planned))
        assert result.outcome is RunOutcome.ERRORED
    else:
        authentik.enable_application.side_effect = RuntimeError("Authentik went away")
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
        return False, "timed out"

    authentik.enable_application.side_effect = ended_meanwhile
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
        return False, "timed out"

    authentik.enable_application.side_effect = rescheduled_meanwhile
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
        return False, "timed out"

    authentik.enable_application.side_effect = cleared_meanwhile
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
