"""Competition configuration services, and starting and stopping the competition."""

import logging
from collections.abc import Callable, Generator
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from django.db import transaction

from core.authentik_utils import set_team_account_active
from core.discord_tasks import BroadcastMessage
from core.models import MAX_TEAM_MEMBERS_LIMIT, AuditLog, CompetitionConfig, DiscordTask
from team.models import MAX_TEAMS, active_team_numbers, team_username

logger = logging.getLogger(__name__)

START_MESSAGE_SENDER = "Black Team"


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

    @property
    def success(self) -> bool:
        return self.error is None

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


def run_competition(enable: bool, actor: str) -> Generator[CompetitionStep, None, CompetitionRunResult]:
    """Start (enable=True) or stop the competition, yielding progress; the one implementation.

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

    # Only the fields this run owns and the schedule times it consumes: other edits made meanwhile survive
    changes: dict[str, object] = {
        "applications_enabled": enable,
        "last_run_incomplete": result.has_failures,
    }
    if enable:
        changes["last_start_accounts_ok"] = result.accounts_ok
        changes["last_start_accounts_tried"] = result.accounts_ok + result.accounts_failed
    with transaction.atomic():
        # Locked so the timer's retries and a concurrent manual start send the start message once.
        locked = CompetitionConfig.locked()
        if (enable and locked.competition_start_time == began_with) or locked.start_passed():
            changes["competition_start_time"] = None
        if locked.end_passed():
            changes["competition_end_time"] = None
            changes["started_this_window"] = False
        if enable:
            changes["started_this_window"] = True
        elif locked.competition_end_time is None:
            changes["started_this_window"] = False
        if enable and not locked.applications_enabled and locked.start_message.strip():
            task = DiscordTask.enqueue(
                BroadcastMessage(target="all-teams", message=locked.start_message, sender=START_MESSAGE_SENDER)
            )
            changes["start_broadcasts"] = [*locked.start_broadcasts, task.pk]
            result.start_message_queued = True
        CompetitionConfig.objects.filter(pk=locked.pk).update(**changes)

    AuditLog.objects.create(
        action="competition_started" if enable else "competition_stopped",
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
        },
    )
    return result


def run_competition_to_completion(
    enable: bool, actor: str, on_step: Callable[[], None] | None = None
) -> CompetitionRunResult:
    """run_competition for callers that don't show progress; on_step runs after each step (a liveness beat)."""
    steps = run_competition(enable, actor)
    while True:
        try:
            next(steps)
        except StopIteration as done:
            result: CompetitionRunResult = done.value
            return result
        if on_step:
            on_step()


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
    actor: str, *, start: datetime | None | Keep = KEEP, end: datetime | None | Keep = KEEP
) -> CompetitionConfig:
    """Set the start and/or end time, None clearing one (or both); a time not given stays. Returns the saved config."""
    config = CompetitionConfig.locked()
    new_start = config.competition_start_time if isinstance(start, Keep) else start
    new_end = config.competition_end_time if isinstance(end, Keep) else end
    if new_start and new_end and new_end <= new_start:
        raise ConfigEditError("The end time must be after the start time")
    changes: dict[str, datetime | None] = {}
    if not isinstance(start, Keep):
        changes["competition_start_time"] = new_start
    if not isinstance(end, Keep):
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
