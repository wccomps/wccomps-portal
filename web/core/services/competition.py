"""Competition configuration services, and starting and stopping the competition."""

import logging
import threading
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from django.db import DEFAULT_DB_ALIAS, connections, transaction
from django.db.models import Q
from django.utils import timezone

from core.authentik_utils import set_team_account_active
from core.discord_tasks import BroadcastMessage
from core.models import AuditLog, CompetitionConfig, DiscordTask
from core.utils import advisory_lock
from team.models import MAX_TEAMS, active_team_numbers, team_username

logger = logging.getLogger(__name__)

START_MESSAGE_SENDER = "Black Team"

# The audit action a start records, with the team accounts it turned on (accounts_ok) and could not (accounts_failed)
COMPETITION_STARTED = "competition_started"
# The audit action a stop records, with the same details
COMPETITION_STOPPED = "competition_stopped"
TIMER_GAVE_UP = "competition_timer_gave_up"
EVENT_CLOSED_OUT = "event_closed_out"

TIMER_ACTOR = "timer"
# Attempts the timer makes at one scheduled start or stop, one a tick, while it is left incomplete or errored
TIMER_ATTEMPTS = 4
# The timer retries only within this long of its first attempt, so a restart never resumes a stale one
TIMER_RETRY_WINDOW = timedelta(hours=1)

# Arbitrary, but fixed; CLAUDE.md says how to free it from a stuck holder
COMPETITION_RUN_LOCK_ID = 7_261_640_003
RUN_IN_PROGRESS = "A close out or another start or stop is running. Try again when it finishes."
RUN_SUPERSEDED = "The schedule or the timer's series changed since the timer planned this attempt"
STEPPED_ACROSS_THREADS = "run_competition must be stepped on the thread that took the run lock"


class RunOutcome(StrEnum):
    REFUSED = "refused"  # Another run or a close out held the run lock, or the timer's plan went stale
    ERRORED = "errored"  # It stopped before changing anything it could record
    INCOMPLETE = "incomplete"  # It left an app or account unchanged
    COMPLETE = "complete"


@dataclass(frozen=True)
class Attempt:
    """The timer's next attempt at a scheduled start (enable=True) or stop; number 1 when the schedule is due."""

    enable: bool
    number: int
    first_started: datetime
    schedule: datetime | None  # The scheduled time the series carries out


@dataclass(frozen=True)
class GiveUp:
    """The timer stops retrying its series; attempt is the last one it made."""

    enable: bool
    attempt: int
    first_started: datetime
    schedule: datetime | None
    reason: str


type TimerPlan = Attempt | GiveUp


@dataclass(frozen=True)
class CompetitionStep:
    """One unit of progress while starting or stopping the competition."""

    message: str
    current: int
    total: int
    ok: bool = True


@dataclass
class CompetitionRunResult:
    """What starting (enable=True) or stopping the competition did."""

    enable: bool
    controlled_apps: list[str] = field(default_factory=list)
    apps_ok: list[str] = field(default_factory=list)
    apps_failed: list[tuple[str, str | None]] = field(default_factory=list)
    accounts_total: int = 0
    accounts_ok: int = 0
    accounts_failed: int = 0
    quotient_synced: bool | None = None
    start_message_queued: bool = False
    error: str | None = None
    refused: bool = False

    @property
    def outcome(self) -> RunOutcome:
        if self.refused:
            return RunOutcome.REFUSED
        if self.error is not None:
            return RunOutcome.ERRORED
        return RunOutcome.INCOMPLETE if self.has_failures else RunOutcome.COMPLETE

    @property
    def has_failures(self) -> bool:
        """Some app binding or team account didn't change: the run finished, but not completely."""
        return bool(self.apps_failed) or self.accounts_failed > 0

    def summary(self) -> str:
        if self.error:
            return self.error
        verb = "enabled" if self.enable else "disabled"
        lines = [f"Applications {verb}: {len(self.apps_ok)}/{len(self.controlled_apps)}"]
        if self.apps_ok:
            lines.append(f"Done: {', '.join(self.apps_ok)}")
        lines.extend(f"Failed: {app}: {error}" for app, error in self.apps_failed)
        accounts = f"Accounts {verb}: {self.accounts_ok}/{self.accounts_total}"
        if self.accounts_failed:
            accounts += f" ({self.accounts_failed} failed)"
        lines.append(accounts)
        if self.quotient_synced is not None:
            what = "Quotient metadata" if self.enable else "Quotient service scores"
            lines.append(f"{what}: {'synced' if self.quotient_synced else 'sync failed'}")
        if self.start_message_queued:
            lines.append("Start message queued for every active team")
        return "\n".join(lines)


def run_competition(
    enable: bool, actor: str, timer: Attempt | None = None
) -> Generator[CompetitionStep, None, CompetitionRunResult]:
    """Start (enable=True) or stop the competition under the run lock, yielding progress (see _run); step it on one
    thread. Refused when the lock is held or the timer's plan went stale."""
    with run_lock() as locked:
        if not locked:
            return CompetitionRunResult(enable=enable, error=RUN_IN_PROGRESS, refused=True)
        return (yield from _locked_run(enable, actor, timer))


def _locked_run(
    enable: bool, actor: str, timer: Attempt | None
) -> Generator[CompetitionStep, None, CompetitionRunResult]:
    owner = threading.get_ident()
    if timer is not None and not _plan_still_holds(timer):
        return CompetitionRunResult(enable=enable, error=RUN_SUPERSEDED, refused=True)
    planned = _current_series()
    steps = _run(enable, actor, timer, planned)
    try:
        while True:
            try:
                step = next(steps)
            except StopIteration as finished:
                result: CompetitionRunResult = finished.value
                break
            yield step
            if threading.get_ident() != owner:
                raise RuntimeError(STEPPED_ACROSS_THREADS)
    except Exception:
        if timer is not None:
            _write_series(timer, planned, _series(timer))
        raise
    if timer is not None and result.outcome is RunOutcome.ERRORED:
        _write_series(timer, planned, _series(timer))
    return result


@contextmanager
def run_lock() -> Iterator[bool]:
    """Try the run lock for this thread's connection, outside any transaction; yields whether it was taken. It is
    released on that connection even when a run is wrongly stepped to its end on another thread, which assumes the
    owning thread is idle meanwhile."""
    db = connections[DEFAULT_DB_ALIAS]
    owner = threading.get_ident()
    elsewhere = False
    try:
        with advisory_lock(COMPETITION_RUN_LOCK_ID, conn=db) as locked:
            try:
                yield locked
            finally:
                elsewhere = threading.get_ident() != owner
                if elsewhere:
                    db.inc_thread_sharing()
    finally:
        if elsewhere:
            db.dec_thread_sharing()


def set_schedule(*, start: datetime | None = None, end: datetime | None = None) -> None:
    """Save a new start and/or end time and end the timer's series for each, in one transaction holding the config
    row, so a timer attempt finishing meanwhile never revives the old series."""
    pk = CompetitionConfig.get_config().pk
    changes: dict[str, object] = {"updated_at": timezone.now()}
    with transaction.atomic():
        CompetitionConfig.objects.select_for_update().filter(pk=pk).values_list("pk").get()
        for enable, when in ((True, start), (False, end)):
            if when is not None:
                end_timer_series(enable)
                changes["competition_start_time" if enable else "competition_end_time"] = when
        CompetitionConfig.objects.filter(pk=pk).update(**changes)


def end_timer_series(enable: bool) -> None:
    """End a live timer series for a start (True) or stop: a new schedule replaces what it was carrying out."""
    CompetitionConfig.objects.filter(pk=CompetitionConfig.get_config().pk, timer_series_enable=enable).update(
        **no_timer_series()
    )


def _plan_still_holds(attempt: Attempt) -> bool:
    config = CompetitionConfig.get_config()
    if attempt.number == 1:
        return config.should_enable_applications() if attempt.enable else config.should_disable_applications()
    return (config.timer_series_enable, config.timer_series_attempt, config.timer_series_started) == (
        attempt.enable,
        attempt.number - 1,
        attempt.first_started,
    )


def _current_series() -> dict[str, object]:
    series: dict[str, object] = (
        CompetitionConfig.objects.filter(pk=CompetitionConfig.get_config().pk).values(*no_timer_series()).get()
    )
    return series


def _write_series(timer: Attempt, planned: dict[str, object], series: dict[str, object]) -> None:
    """Record series, unless the series changed since the timer planned this attempt or an admin set a new time for
    its direction meanwhile."""
    schedule = "competition_start_time" if timer.enable else "competition_end_time"
    kept = Q(**{schedule: timer.schedule})
    if timer.number > 1:
        # Attempt 1 cleared it; until then, clearing it (a cleanup) ends the series too
        kept |= Q(**{f"{schedule}__isnull": True})
    CompetitionConfig.objects.filter(kept, pk=CompetitionConfig.get_config().pk, **planned).update(**series)


def no_timer_series() -> dict[str, object]:
    """The fields of a config with no timer series."""
    return {
        "timer_series_enable": None,
        "timer_series_attempt": 0,
        "timer_series_started": None,
        "timer_series_schedule": None,
    }


def _series(attempt: Attempt) -> dict[str, object]:
    return {
        "timer_series_enable": attempt.enable,
        "timer_series_attempt": attempt.number,
        "timer_series_started": attempt.first_started,
        "timer_series_schedule": attempt.schedule,
    }


def _run(
    enable: bool, actor: str, timer: Attempt | None, planned: dict[str, object]
) -> Generator[CompetitionStep, None, CompetitionRunResult]:
    """Start (enable=True) or stop the competition, yielding progress.

    Toggles each controlled application's BlueTeam binding and the team accounts (start enables the
    active teams' accounts; stop disables every team account), refreshes stored groups so permissions
    follow at once, syncs Quotient metadata on start or final service scores on stop, then records the
    new state and an audit entry. A start that turns the competition on also queues the start message
    to every active team. Blocking (Authentik and Quotient HTTP): async callers run it in a thread.
    """
    from django.contrib.auth.models import User
    from scoring.quotient_sync import sync_quotient_metadata, sync_service_scores

    from core.authentik_manager import AuthentikAPIError, AuthentikManager, reason_for
    from core.services.user_groups import refresh_user_groups

    config = CompetitionConfig.get_config()
    result = CompetitionRunResult(enable=enable, controlled_apps=list(config.controlled_applications))
    if not result.controlled_apps:
        result.error = "No controlled applications configured"
        return result

    verb = "Enabled" if enable else "Disabled"
    # Inactive teams' accounts stay in their groups between events, so start must not enable them. Stop
    # disables every team account, closing any that was left enabled.
    numbers = active_team_numbers() if enable else list(range(1, MAX_TEAMS + 1))
    result.accounts_total = len(numbers)
    total = len(result.controlled_apps) + len(numbers) + 2
    step = 0
    manager = AuthentikManager()

    for slug in result.controlled_apps:
        ok, error = manager.enable_application(slug) if enable else manager.disable_application(slug)
        step += 1
        if ok:
            result.apps_ok.append(slug)
            yield CompetitionStep(f"{verb} {slug}", step, total)
        else:
            result.apps_failed.append((slug, error))
            yield CompetitionStep(f"Failed {slug}: {error}", step, total, ok=False)

    for number in numbers:
        username = team_username(number)
        ok, why = set_team_account_active(manager, number, enable)
        step += 1
        if ok:
            result.accounts_ok += 1
        else:
            result.accounts_failed += 1
        yield CompetitionStep(f"{verb} {username}" if ok else f"Failed {username}: {why}", step, total, ok=ok)

    # Team accounts' groups follow is_active; don't make teams wait for the periodic refresh.
    step += 1
    try:
        refresh_user_groups(manager)
        yield CompetitionStep("Refreshed team permissions", step, total)
    except Exception as e:
        logger.warning(f"Group refresh after competition {'start' if enable else 'stop'} failed: {e}")
        why = reason_for(e) if isinstance(e, AuthentikAPIError) else str(e)
        yield CompetitionStep(f"Permission refresh failed (retried within 5 minutes): {why}", step, total, ok=False)

    step += 1
    actor_user = User.objects.filter(username=actor).first()
    if enable:
        try:
            sync_quotient_metadata(actor_user)
            result.quotient_synced = True
            yield CompetitionStep("Quotient metadata synced", step, total)
        except Exception as e:
            logger.warning(f"Failed to sync Quotient metadata: {e}")
            result.quotient_synced = False
            yield CompetitionStep(f"Quotient sync failed: {e}", step, total, ok=False)
    else:
        try:
            sync_service_scores(actor_user)
            result.quotient_synced = True
            yield CompetitionStep("Quotient service scores synced", step, total)
        except Exception as e:
            logger.warning(f"Failed to sync Quotient service scores: {e}")
            result.quotient_synced = False
            yield CompetitionStep(f"Quotient score sync failed: {e}", step, total, ok=False)

    # Only the fields this run owns: a schedule or app-list edit made meanwhile must survive.
    changes: dict[str, object] = {"applications_enabled": enable}
    schedule = "competition_start_time" if enable else "competition_end_time"
    if timer is None:
        changes |= {schedule: None, **no_timer_series()}
    with transaction.atomic():
        # Locked so the timer's retries and a concurrent manual start send the start message once.
        was_enabled, start_message = (
            CompetitionConfig.objects.select_for_update()
            .values_list("applications_enabled", "start_message")
            .get(pk=config.pk)
        )
        CompetitionConfig.objects.filter(pk=config.pk).update(**changes)
        if timer is not None:
            left = _series(timer) if result.outcome is RunOutcome.INCOMPLETE else no_timer_series()
            _write_series(timer, planned, left)
            # Only the schedule the series carries out: a time set since is the admin's
            CompetitionConfig.objects.filter(pk=config.pk, **{schedule: timer.schedule}).update(**{schedule: None})
        if enable and not was_enabled and start_message.strip():
            DiscordTask.enqueue(
                BroadcastMessage(target="all-teams", message=start_message, sender=START_MESSAGE_SENDER)
            )
            result.start_message_queued = True

        AuditLog.objects.create(
            action=COMPETITION_STARTED if enable else COMPETITION_STOPPED,
            admin_user=actor,
            target_entity="competition_config",
            target_id=config.pk,
            details={
                "apps_ok": result.apps_ok,
                "apps_failed": [app for app, _ in result.apps_failed],
                "accounts_ok": result.accounts_ok,
                "accounts_failed": result.accounts_failed,
                "quotient_synced": result.quotient_synced,
                "start_message_queued": result.start_message_queued,
                **({"timer_attempt": timer.number} if timer is not None else {}),
            },
        )
    return result


def last_start_accounts() -> tuple[int, int] | None:
    """The team accounts the last recorded start turned on, and how many it tried; None when none is recorded."""
    details = (
        AuditLog.objects.filter(action=COMPETITION_STARTED).order_by("-created_at").values_list("details", flat=True)
    ).first()
    if not details or "accounts_ok" not in details or "accounts_failed" not in details:
        return None
    return details["accounts_ok"], details["accounts_ok"] + details["accounts_failed"]


def incomplete_run() -> bool | None:
    """Whether the last recorded run was a start (True) or a stop (False) that left an app or team account
    unchanged, so it needs running again; None when it changed everything, or no run is recorded."""
    last = (
        AuditLog.objects.filter(action__in=(COMPETITION_STARTED, COMPETITION_STOPPED))
        .order_by("-created_at")
        .values_list("action", "details")
        .first()
    )
    if last is None:
        return None
    action, details = last
    if not details.get("apps_failed") and not details.get("accounts_failed"):
        return None
    return action == COMPETITION_STARTED


def current_timer_plan(now: datetime) -> TimerPlan | None:
    """The timer's next step: attempt 1 at a due schedule, the series' next attempt, or giving up past TIMER_ATTEMPTS
    or TIMER_RETRY_WINDOW; None for nothing."""
    config = CompetitionConfig.get_config()
    series = config.timer_series_enable
    for due in (True, False):
        is_due = config.should_enable_applications() if due else config.should_disable_applications()
        if is_due and series is not due:
            return Attempt(due, 1, now, config.competition_start_time if due else config.competition_end_time)
    if series is None or config.timer_series_started is None:
        return None
    started, made, schedule = config.timer_series_started, config.timer_series_attempt, config.timer_series_schedule
    if made >= TIMER_ATTEMPTS:
        return GiveUp(series, made, started, schedule, f"still incomplete after {TIMER_ATTEMPTS} attempts")
    if now - started > TIMER_RETRY_WINDOW:
        minutes = int(TIMER_RETRY_WINDOW.total_seconds() // 60)
        return GiveUp(series, made, started, schedule, f"its first attempt was over {minutes} minutes ago")
    return Attempt(series, made + 1, started, schedule)


def give_up_timer_series(plan: GiveUp) -> bool:
    """End the series the timer gives up on, and drop the schedule it carried out so it doesn't start over, unless a
    run or Close Out changed the series, or an admin the schedule, meanwhile; whether it ended the series."""
    schedule = "competition_start_time" if plan.enable else "competition_end_time"
    pk = CompetitionConfig.get_config().pk
    with transaction.atomic():
        ended = CompetitionConfig.objects.filter(
            pk=pk,
            timer_series_enable=plan.enable,
            timer_series_attempt=plan.attempt,
            timer_series_started=plan.first_started,
        ).update(**no_timer_series())
        if ended:
            CompetitionConfig.objects.filter(pk=pk, **{schedule: plan.schedule}).update(**{schedule: None})
            AuditLog.objects.create(
                action=TIMER_GAVE_UP,
                admin_user=TIMER_ACTOR,
                target_entity="competition_config",
                target_id=CompetitionConfig.get_config().pk,
                details={"enable": plan.enable, "attempts": plan.attempt, "reason": plan.reason},
            )
    return bool(ended)
