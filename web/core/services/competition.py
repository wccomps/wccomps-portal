"""Competition configuration services, and starting and stopping the competition."""

import logging
from collections.abc import Callable, Generator
from dataclasses import dataclass, field

from django.db import transaction

from core.discord_tasks import BroadcastMessage
from core.models import AuditLog, CompetitionConfig, DiscordTask
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
            lines.append(f"✓ {', '.join(self.apps_ok)}")
        lines.extend(f"✗ {app}: {error}" for app, error in self.apps_failed)
        accounts = f"Accounts {verb}: {self.accounts_ok}/{self.accounts_total}"
        if self.accounts_failed:
            accounts += f" ({self.accounts_failed} failed)"
        lines.append(accounts)
        if self.quotient_synced is not None:
            lines.append(f"Quotient metadata: {'synced' if self.quotient_synced else 'sync failed'}")
        if self.start_message_queued:
            lines.append("Start message queued for every active team")
        return "\n".join(lines)


def run_competition(enable: bool, actor: str) -> Generator[CompetitionStep, None, CompetitionRunResult]:
    """Start (enable=True) or stop the competition, yielding progress; the one implementation.

    Toggles each controlled application's BlueTeam binding and the team accounts (start enables the
    active teams' accounts; stop disables every team account), refreshes stored groups so permissions
    follow at once, syncs Quotient metadata on start, then records the new state and an audit entry.
    A start that turns the competition on also queues the start message to every active team.
    Blocking (Authentik HTTP): async callers run it in a thread.
    """
    from scoring.quotient_sync import sync_quotient_metadata

    from core.authentik_manager import AuthentikManager
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
    total = len(result.controlled_apps) + len(numbers) + 1 + (1 if enable else 0)
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
        ok, _ = manager.toggle_user(username, is_active=enable)
        step += 1
        if ok:
            result.accounts_ok += 1
        else:
            result.accounts_failed += 1
        yield CompetitionStep(f"{verb} {username}" if ok else f"Failed {username}", step, total, ok=ok)

    # Team accounts' groups follow is_active; don't make teams wait for the periodic refresh.
    step += 1
    try:
        refresh_user_groups(manager)
        yield CompetitionStep("Refreshed team permissions", step, total)
    except Exception as e:
        logger.warning(f"Group refresh after competition {'start' if enable else 'stop'} failed: {e}")
        yield CompetitionStep(f"Permission refresh failed (retried within 5 minutes): {e}", step, total, ok=False)

    if enable:
        step += 1
        try:
            sync_quotient_metadata()
            result.quotient_synced = True
            yield CompetitionStep("Quotient metadata synced", step, total)
        except Exception as e:
            logger.warning(f"Failed to sync Quotient metadata: {e}")
            result.quotient_synced = False
            yield CompetitionStep(f"Quotient sync failed: {e}", step, total, ok=False)

    # Only the fields this run owns: a schedule or app-list edit made meanwhile must survive.
    changes: dict[str, object] = {"applications_enabled": enable}
    changes["competition_start_time" if enable else "competition_end_time"] = None
    with transaction.atomic():
        # Locked so the timer's retries and a concurrent manual start send the start message once.
        was_enabled, start_message = (
            CompetitionConfig.objects.select_for_update()
            .values_list("applications_enabled", "start_message")
            .get(pk=config.pk)
        )
        CompetitionConfig.objects.filter(pk=config.pk).update(**changes)
        if enable and not was_enabled and start_message.strip():
            DiscordTask.enqueue(
                BroadcastMessage(target="all-teams", message=start_message, sender=START_MESSAGE_SENDER)
            )
            result.start_message_queued = True

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
