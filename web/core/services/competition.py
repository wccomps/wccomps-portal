"""Competition configuration services, and starting and stopping the competition."""

import logging
import threading
from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, StrEnum

from django.db import DEFAULT_DB_ALIAS, connections, transaction
from django.db.models import Q

from core.authentik_utils import set_team_account_active
from core.discord_tasks import BroadcastMessage
from core.models import MAX_TEAM_MEMBERS_LIMIT, AuditLog, CompetitionConfig, DiscordTask
from core.utils import advisory_lock, format_competition_time
from team.models import MAX_TEAMS, active_team_numbers, team_username

logger = logging.getLogger(__name__)

START_MESSAGE_SENDER = "Black Team"
COUNTDOWN_SENDER = "Gold Team"

# The audit action a start records, with the team accounts it turned on (accounts_ok) and could not (accounts_failed)
COMPETITION_STARTED = "competition_started"
# The audit action a stop records, with the same details
COMPETITION_STOPPED = "competition_stopped"
TIMER_GAVE_UP = "competition_timer_gave_up"
COMPETITION_COUNTDOWN_QUEUED = "competition_countdown_queued"

TIMER_ACTOR = "timer"
# Attempts the timer makes at one scheduled start or stop, one a tick, while it is left incomplete or errored
TIMER_ATTEMPTS = 4
# The timer retries only within this long of its first attempt, so a restart never resumes a stale one
TIMER_RETRY_WINDOW = timedelta(hours=1)
COUNTDOWN_WINDOW = timedelta(minutes=5)
COUNTDOWN_MILESTONES: tuple[tuple[int, str], ...] = (
    (60, "1 hour"),
    (30, "30 minutes"),
)

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
    account_failures: list[tuple[str, str]] = field(default_factory=list)
    nextcloud_ok: int = 0
    nextcloud_failed: list[tuple[str, str]] = field(default_factory=list)
    quotient_synced: bool | None = None
    service_max_note: str | None = None
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
        """Some app binding, team account or Nextcloud account didn't change: the run finished, but not completely."""
        return bool(self.apps_failed) or self.accounts_failed > 0 or bool(self.nextcloud_failed)

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
        lines.extend(f"Failed: {username}: {error}" for username, error in self.account_failures)
        if self.nextcloud_ok or self.nextcloud_failed:
            nc_total = self.nextcloud_ok + len(self.nextcloud_failed)
            lines.append(f"Nextcloud accounts provisioned: {self.nextcloud_ok}/{nc_total}")
            lines.extend(f"Failed Nextcloud: {username}: {error}" for username, error in self.nextcloud_failed)
        if self.quotient_synced is not None:
            what = "Quotient metadata" if self.enable else "Quotient service scores"
            lines.append(f"{what}: {'synced' if self.quotient_synced else 'sync failed'}")
        if self.service_max_note:
            lines.append(self.service_max_note)
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
    new state and an audit entry. The first start of an event also queues the start message to every
    active team. Blocking (Authentik, Nextcloud and Quotient HTTP): async callers run it in a thread.
    """
    from django.contrib.auth.models import User
    from scoring.quotient_sync import sync_quotient_metadata, sync_service_scores

    from core.authentik_manager import AuthentikAPIError, AuthentikManager, reason_for
    from core.nextcloud import NextcloudAPIError, NextcloudManager, nextcloud_configured
    from core.nextcloud import reason_for as nextcloud_reason_for
    from core.services.user_groups import refresh_user_groups

    config = CompetitionConfig.get_config()
    began_with = config.competition_start_time
    result = CompetitionRunResult(enable=enable, controlled_apps=list(config.controlled_applications))
    if not result.controlled_apps:
        result.error = "No controlled applications configured"
        return result

    verb = "Enabled" if enable else "Disabled"
    # Inactive teams' accounts stay in their groups between events, so start must not enable them. Stop
    # disables every team account, closing any that was left enabled.
    numbers = active_team_numbers() if enable else list(range(1, MAX_TEAMS + 1))
    result.accounts_total = len(numbers)
    provision_nextcloud = enable and nextcloud_configured()
    total = len(result.controlled_apps) + len(numbers) + 2 + (1 if provision_nextcloud else 0)
    step = 0
    with AuthentikManager() as manager:
        for slug in result.controlled_apps:
            step += 1
            try:
                manager.set_application_enabled(slug, enable)
            except AuthentikAPIError as e:
                logger.warning(f"Failed to {'enable' if enable else 'disable'} application '{slug}': {e}")
                why = reason_for(e)
                result.apps_failed.append((slug, why))
                yield CompetitionStep(f"Failed {slug}: {why}", step, total, ok=False)
            else:
                result.apps_ok.append(slug)
                yield CompetitionStep(f"{verb} {slug}", step, total)

        for number in numbers:
            username = team_username(number)
            step += 1
            try:
                set_team_account_active(manager, number, enable)
            except AuthentikAPIError as e:
                logger.warning(f"Failed to set {username} active={enable}: {e}")
                why = reason_for(e)
                result.accounts_failed += 1
                result.account_failures.append((username, why))
                yield CompetitionStep(f"Failed {username}: {why}", step, total, ok=False)
            else:
                result.accounts_ok += 1
                yield CompetitionStep(f"{verb} {username}", step, total)

        # Permissions follow each account's enabled state; don't make teams wait for the periodic refresh.
        step += 1
        try:
            refresh_user_groups(manager)
            yield CompetitionStep("Refreshed team permissions", step, total)
        except Exception as e:
            logger.warning(f"Group refresh after competition {'start' if enable else 'stop'} failed: {e}")
            why = reason_for(e) if isinstance(e, AuthentikAPIError) else str(e)
            yield CompetitionStep(f"Permission refresh failed (retried within 5 minutes): {why}", step, total, ok=False)

    if provision_nextcloud:
        step += 1
        with NextcloudManager() as nextcloud:
            for number in numbers:
                username = team_username(number)
                try:
                    nextcloud.provision_team_user(number)
                except NextcloudAPIError as e:
                    logger.warning(f"Failed to provision Nextcloud user {username}: {e}")
                    result.nextcloud_failed.append((username, nextcloud_reason_for(e)))
                else:
                    result.nextcloud_ok += 1
        if result.nextcloud_failed:
            failed = ", ".join(f"{u} ({w})" for u, w in result.nextcloud_failed)
            yield CompetitionStep(
                f"Nextcloud provisioned {result.nextcloud_ok}/{len(numbers)} teams (failed: {failed})",
                step,
                total,
                ok=False,
            )
        else:
            yield CompetitionStep(f"Nextcloud provisioned ({result.nextcloud_ok} teams)", step, total)

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
            note = sync_service_scores(actor_user)["max_not_updated"]
            result.quotient_synced = True
            result.service_max_note = note
            done = "Quotient service scores synced"
            yield CompetitionStep(f"{done}. {note}" if note else done, step, total, ok=note is None)
        except Exception as e:
            logger.warning(f"Failed to sync Quotient service scores: {e}")
            result.quotient_synced = False
            yield CompetitionStep(f"Quotient score sync failed: {e}", step, total, ok=False)

    # Only the fields this run owns and the schedule times it consumes: other edits made meanwhile survive
    changes: dict[str, object] = {
        "applications_enabled": enable,
        "last_run_incomplete": result.has_failures,
    }
    if enable:
        changes["last_start_accounts_ok"] = result.accounts_ok
        changes["last_start_accounts_tried"] = result.accounts_ok + result.accounts_failed
    if timer is None:
        changes |= no_timer_series()
    expected_start = began_with if timer is None else timer.schedule
    with transaction.atomic():
        # Locked so the timer's retries and a concurrent manual start send the start message once.
        locked = CompetitionConfig.locked()
        if timer is not None:
            left = _series(timer) if result.outcome is RunOutcome.INCOMPLETE else no_timer_series()
            _write_series(timer, planned, left)
        if (enable and locked.competition_start_time == expected_start) or locked.start_passed():
            changes["competition_start_time"] = None
        if (not enable and timer is not None and locked.competition_end_time == timer.schedule) or locked.end_passed():
            changes["competition_end_time"] = None
        if enable:
            changes["started_this_window"] = True
        elif locked.end_passed() or locked.competition_end_time is None or "competition_end_time" in changes:
            changes["started_this_window"] = False
        if enable and not locked.applications_enabled and not locked.start_broadcasts and locked.start_message.strip():
            task = DiscordTask.enqueue(
                BroadcastMessage(target="all-teams", message=locked.start_message, sender=START_MESSAGE_SENDER)
            )
            changes["start_broadcasts"] = [task.pk]
            result.start_message_queued = True
        CompetitionConfig.objects.filter(pk=locked.pk).update(**changes)

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
                "account_failures": dict(result.account_failures),
                "quotient_synced": result.quotient_synced,
                **({"service_max_note": result.service_max_note} if result.service_max_note else {}),
                "start_message_queued": result.start_message_queued,
                **(
                    {"nextcloud_ok": result.nextcloud_ok, "nextcloud_failed": dict(result.nextcloud_failed)}
                    if provision_nextcloud
                    else {}
                ),
                **({"timer_attempt": timer.number} if timer is not None else {}),
            },
        )
    return result


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


def competition_schedule_context() -> dict[str, object]:
    config = CompetitionConfig.get_config()
    start = config.competition_start_time
    end = config.competition_end_time
    return {
        "competition_start_time": start,
        "competition_start_time_display": format_competition_time(start) if start else "",
        "competition_end_time": end,
        "competition_end_time_display": format_competition_time(end) if end else "",
    }


def _due_countdown_milestone(config: CompetitionConfig, now: datetime) -> tuple[int, str] | None:
    if not config.applications_enabled or config.competition_end_time is None:
        return None
    remaining = config.competition_end_time - now
    for minutes, label in COUNTDOWN_MILESTONES:
        threshold = timedelta(minutes=minutes)
        if threshold - COUNTDOWN_WINDOW <= remaining <= threshold:
            return minutes, label
    return None


def queue_due_countdown(now: datetime) -> str | None:
    if _due_countdown_milestone(CompetitionConfig.get_config(), now) is None:
        return None
    with transaction.atomic():
        config = CompetitionConfig.locked()
        milestone = _due_countdown_milestone(config, now)
        if milestone is None or config.competition_end_time is None:
            return None
        minutes, label = milestone
        end_time = config.competition_end_time
        end_iso = end_time.isoformat()
        if AuditLog.objects.filter(
            action=COMPETITION_COUNTDOWN_QUEUED,
            details__end_time=end_iso,
            details__minutes_remaining=minutes,
        ).exists():
            return None
        verb = "is" if minutes == 60 else "are"
        end_display = format_competition_time(end_time)
        message = (
            f"There {verb} {label} remaining in the competition. "
            f"The competition ends at {end_display} (<t:{int(end_time.timestamp())}:t>)."
        )
        task = DiscordTask.enqueue(BroadcastMessage(target="all-teams", message=message, sender=COUNTDOWN_SENDER))
        AuditLog.objects.create(
            action=COMPETITION_COUNTDOWN_QUEUED,
            admin_user=TIMER_ACTOR,
            target_entity="competition_config",
            target_id=config.pk,
            details={
                "end_time": end_iso,
                "minutes_remaining": minutes,
                "task_id": task.pk,
            },
        )
        return label


class ConfigEditError(ValueError):
    """A competition setting was refused; the message says why."""


def _audit_config(action: str, actor: str, config: CompetitionConfig, details: dict[str, object]) -> None:
    AuditLog.objects.create(
        action=action, admin_user=actor, target_entity="competition_config", target_id=config.pk, details=details
    )


@transaction.atomic
def set_max_members(max_members: int, actor: str) -> int:
    """Set how many members a team may link; returns the old limit."""
    if not 1 <= max_members <= MAX_TEAM_MEMBERS_LIMIT:
        raise ConfigEditError(f"Max members must be 1-{MAX_TEAM_MEMBERS_LIMIT}")
    config = CompetitionConfig.locked()
    old_max = config.max_team_members
    config.max_team_members = max_members
    config.save(update_fields=["max_team_members", "updated_at"])
    _audit_config("max_team_members_updated", actor, config, {"old_max": old_max, "new_max": max_members})
    return old_max


@transaction.atomic
def set_start_message(message: str, actor: str) -> None:
    """Set the start message; its audit records only the length, since it usually carries credentials."""
    config = CompetitionConfig.locked()
    config.start_message = message
    config.save(update_fields=["start_message", "updated_at"])
    _audit_config("competition_start_message_updated", actor, config, {"length": len(message)})


class Keep(Enum):
    """set_schedule's default for a time it leaves as stored."""

    KEEP = "keep"


KEEP = Keep.KEEP


@transaction.atomic
def set_schedule(
    actor: str = "", *, start: datetime | None | Keep = KEEP, end: datetime | None | Keep = KEEP
) -> CompetitionConfig:
    """Set the start and/or end time, None clearing one (or both); a time not given stays. Returns the saved config."""
    config = CompetitionConfig.locked()
    new_start = config.competition_start_time if isinstance(start, Keep) else start
    new_end = config.competition_end_time if isinstance(end, Keep) else end
    if new_start and new_end and new_end <= new_start:
        raise ConfigEditError("The end time must be after the start time")
    changes: dict[str, datetime | None] = {}
    if not isinstance(start, Keep):
        end_timer_series(True)
        changes["competition_start_time"] = new_start
    if not isinstance(end, Keep):
        end_timer_series(False)
        changes["competition_end_time"] = new_end
    for name, value in changes.items():
        setattr(config, name, value)
    window_reset = isinstance(start, datetime) or end is None
    if window_reset:
        config.started_this_window = False
    config.save(update_fields=[*changes, *(["started_this_window"] if window_reset else []), "updated_at"])
    details = {
        name.removeprefix("competition_"): value.isoformat() if value else None for name, value in changes.items()
    }
    _audit_config(
        "competition_schedule_set", actor, config, {**details, "controlled_apps": config.controlled_applications}
    )
    return config


@transaction.atomic
def clear_missed_window(actor: str) -> str:
    """Clear an end that passed while the competition was stopped, with any start before it. Returns "" when there was
    none, END_AFTER_STOP when a start ran in this window (a manual stop came before the end), else MISSED_WINDOW."""
    config = CompetitionConfig.locked()
    if not config.stale_end():
        return ""
    kind = END_AFTER_STOP if config.started_this_window else MISSED_WINDOW
    set_schedule(actor, start=None, end=None)
    return kind


MISSED_WINDOW = "missed_window"
END_AFTER_STOP = "end_after_stop"


@transaction.atomic
def set_controlled_apps(change: Callable[[list[str]], list[str]], actor: str, **details: object) -> list[str]:
    """Store change(the stored list of controlled app slugs), read under the row lock; returns the new list."""
    config = CompetitionConfig.locked()
    apps = change(list(config.controlled_applications or []))
    config.controlled_applications = apps
    config.save(update_fields=["controlled_applications", "updated_at"])
    _audit_config("competition_apps_configured", actor, config, {"controlled_apps": apps, **details})
    return apps
