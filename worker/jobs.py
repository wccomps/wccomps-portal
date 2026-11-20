"""The worker's periodic jobs: the competition timer, the Authentik group refresh, the Quotient sync, the preflight
report and the expired session cleanup.

None of them talks to a chat system: what they announce is queued (notify_ops, the competition service's status
refreshes). Each loop runs a pass at once, then one each interval from the start of the last, and advances its
heartbeat (core.heartbeat) after each pass that completes; a pass that raises is logged and skips the beat.
"""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from asgiref.sync import sync_to_async
from django.core.management import call_command
from django.utils import timezone
from quotient.client import get_quotient_client
from scoring.preflight import preflight_report
from scoring.quotient_sync import sync_quotient_metadata

from core.authentik_manager import AuthentikManager
from core.heartbeat import GROUPS, PREFLIGHT, QUOTIENT, SESSIONS, TIMER, beat, beat_for
from core.models import CompetitionConfig
from core.services.competition import (
    END_AFTER_STOP,
    TIMER_ACTOR,
    TIMER_ATTEMPTS,
    Attempt,
    GiveUp,
    RunOutcome,
    arun_competition,
    clear_missed_window,
    current_timer_plan,
    give_up_timer_series,
    queue_due_countdown,
)
from core.services.ops import notify_ops
from core.services.user_groups import (
    GroupRefreshAbortedError,
    GroupRefreshResult,
    refresh_user_groups_unless_running,
)
from core.utils import recycle_db_connection, run_blocking, split_message

logger = logging.getLogger(__name__)

TIMER_SECONDS = 60
REFRESH_MINUTES = 5
SYNC_MINUTES = 5
# Quotient is routinely off between competitions; check less often and log state changes only
BACKOFF_MINUTES = 15
SESSION_CLEANUP_MINUTES = 60
PREFLIGHT_SECONDS = 60
# Minutes before the scheduled start at which the preflight report is posted
PREFLIGHT_REPORT_MINUTES = (30, 10)
# Discord's message limit, the lowest of the chat systems'
OPS_MESSAGE_CHARS = 2000


async def _every(name: str, seconds: Callable[[], float], run_pass: Callable[[], Awaitable[None]]) -> None:
    """Run a pass now, then each seconds() from the start of the last one (at once after a pass that overran). Each
    pass that completes advances the heartbeat name, as does code it awaits that calls beat(); one that raises is
    logged and skips the beat, and the loop goes on."""
    beat_for(name)
    loop = asyncio.get_running_loop()
    while True:
        started = loop.time()
        try:
            await run_pass()
        except Exception as e:
            logger.exception(f"Error in the {name} pass: {e}")
        else:
            beat()
        await asyncio.sleep(max(0.0, started + seconds() - loop.time()))


# ---------------------------------------------------------------------------------------------------------------
# Competition timer: enables and disables the competition at its scheduled times
# ---------------------------------------------------------------------------------------------------------------


async def notify(message: str) -> None:
    """Queue an ops notice, so the timer never waits on chat. The competition service queued any status refresh with
    the change itself."""
    await sync_to_async(notify_ops)(message, actor=TIMER_ACTOR)


async def competition_timer_loop() -> None:
    await _every(TIMER, lambda: TIMER_SECONDS, timer_pass)


async def timer_pass() -> None:
    """Check the schedule once. The steps of a run in progress advance the timer heartbeat too."""
    await recycle_db_connection()
    await check_competition_times()


async def check_competition_times() -> None:
    # Errors here propagate so the pass skips its heartbeat: a timer that can't read the
    # config is not alive in any useful sense.
    now = timezone.now()
    match await sync_to_async(current_timer_plan)(now):
        case None:
            if await sync_to_async(lambda: CompetitionConfig.get_config().stale_end())():
                await _clear_missed_window()
            elif label := await sync_to_async(queue_due_countdown)(now):
                await notify(
                    f"**Competition Countdown:** queued the {label} remaining announcement for all active teams."
                )
        case GiveUp() as plan:
            await _give_up(plan)
        case Attempt() as attempt:
            await _attempt(attempt)


async def _clear_missed_window() -> None:
    # Off the shared thread (the lock waits out a Close Out), and caught so the heartbeat keeps beating
    try:
        cleared = await run_blocking(clear_missed_window, TIMER_ACTOR)
    except Exception:
        logger.exception("Could not clear a missed competition window")
        return
    if cleared == END_AFTER_STOP:
        await notify("**Schedule cleared:** the scheduled end passed after a manual stop.")
    elif cleared:
        await notify(
            "**Schedule cleared:** its end passed while the competition was stopped and would have refused "
            "a new start time. Set a new schedule on the Competition setup page."
        )


async def _give_up(plan: GiveUp) -> None:
    action = "Start" if plan.enable else "Stop"
    if not await sync_to_async(give_up_timer_series)(plan):
        return
    await notify(
        f"**Competition Auto-{action} Incomplete:** the timer stopped retrying, as {plan.reason}. "
        f"Run {action} again from the competition page."
    )


async def _attempt(attempt: Attempt) -> None:
    action, done = ("Start", "Started") if attempt.enable else ("Stop", "Stopped")
    logger.info(f"Competition {action.lower()} attempt {attempt.number} of {TIMER_ATTEMPTS}")
    try:
        result = await arun_competition(attempt.enable, actor=TIMER_ACTOR, timer=attempt)
    except Exception as e:
        logger.exception(f"Competition {action.lower()} attempt {attempt.number} failed: {e}")
        if attempt.number == 1:
            with contextlib.suppress(Exception):
                await notify(f"**Error in competition timer:** {e}")
        return
    match result.outcome:
        case RunOutcome.REFUSED:
            logger.info(f"Competition {action.lower()} attempt skipped: {result.error}")
        case RunOutcome.ERRORED:
            if attempt.number == 1:
                await notify(f"**Competition Auto-{action} Failed:** {result.error}")
        case RunOutcome.INCOMPLETE:
            if attempt.number == 1:
                await notify(
                    f"**Competition Auto-{done}, Incomplete:** retrying each minute, up to {TIMER_ATTEMPTS} "
                    f"attempts.\n\n{result.summary()}"
                )
        case RunOutcome.COMPLETE:
            message = f"**Competition Auto-{done}!**\n\n{result.summary()}"
            if attempt.number > 1:
                message += f"\n\nCompleted on attempt {attempt.number}."
            await notify(message)


# ---------------------------------------------------------------------------------------------------------------
# Group refresh: keeps UserGroups in step with Authentik between logins
# ---------------------------------------------------------------------------------------------------------------


def refresh_groups() -> GroupRefreshResult | None:
    with AuthentikManager() as manager:
        return refresh_user_groups_unless_running(manager)


async def group_refresh_loop() -> None:
    await _every(GROUPS, lambda: REFRESH_MINUTES * 60, group_refresh_pass)


async def group_refresh_pass() -> None:
    """Refresh once. Authentik failing is logged, not raised: the loop is alive, and restarting the worker would not
    bring Authentik back. The chat process's role sync follows the groups that changed (bot.cogs.role_sync)."""
    try:
        result = await run_blocking(refresh_groups)
    except GroupRefreshAbortedError as e:
        logger.error(f"Authentik group refresh skipped, nothing changed: {e}")
    except Exception as e:
        logger.warning(f"Authentik group refresh failed: {e}")
    else:
        if result is None:
            logger.info("Authentik group refresh skipped: another refresh is running")
        elif result.changed:
            logger.info(f"Authentik group refresh: {result.changed} of {result.checked} users changed")


# ---------------------------------------------------------------------------------------------------------------
# Quotient sync: refreshes the Quotient metadata snapshot the ticket forms read
# ---------------------------------------------------------------------------------------------------------------


class QuotientSync:
    """Syncs Quotient metadata every SYNC_MINUTES, or BACKOFF_MINUTES while Quotient is unavailable."""

    def __init__(self) -> None:
        self.quotient_unavailable = False

    @property
    def interval_minutes(self) -> int:
        return BACKOFF_MINUTES if self.quotient_unavailable else SYNC_MINUTES

    async def run_pass(self) -> None:
        """Sync once. Quotient being unavailable is logged, not raised: the loop is alive and backing off, and
        restarting the worker would not bring Quotient back."""
        try:
            await run_blocking(sync_quotient_metadata)
        except ValueError as e:
            if self.quotient_unavailable:
                logger.debug(f"Quotient still unavailable: {e}")
            else:
                self.quotient_unavailable = True
                logger.warning(f"Quotient sync failed: {e}; retrying every {BACKOFF_MINUTES} minutes")
            return
        except Exception as e:
            logger.exception(f"Error syncing Quotient metadata: {e}")
            return

        if self.quotient_unavailable:
            self.quotient_unavailable = False
            logger.info(f"Quotient reachable again; syncing every {SYNC_MINUTES} minutes")
        logger.debug("Quotient metadata synced")


async def quotient_sync_loop() -> None:
    sync = QuotientSync()
    await _every(QUOTIENT, lambda: sync.interval_minutes * 60, sync.run_pass)


# ---------------------------------------------------------------------------------------------------------------
# Preflight report: Quotient's latest preflight round, posted to ops before the scheduled start
# ---------------------------------------------------------------------------------------------------------------


def read_preflight(now: datetime) -> str:
    client = get_quotient_client()
    status = client.get_engine_status()
    return preflight_report(status, client.get_team_names() if status else None, now)


class PreflightReports:
    """Queues the preflight report at each of PREFLIGHT_REPORT_MINUTES before the scheduled start, once per start time
    (in this process: a worker restarted inside a report's window posts it again)."""

    def __init__(self) -> None:
        # (scheduled start, minutes before it) for each report time posted or covered by a later report
        self.sent: set[tuple[datetime, int]] = set()

    async def run_pass(self) -> None:
        """Queue the report if one is due. Quotient being unavailable is reported, not raised; a database that can't be
        read raises, so the pass skips its heartbeat."""
        await recycle_db_connection()
        config = await sync_to_async(CompetitionConfig.get_config)()
        now = timezone.now()
        start = config.competition_start_time
        if not start or config.applications_enabled or now >= start:
            return
        due = [m for m in PREFLIGHT_REPORT_MINUTES if now >= start - timedelta(minutes=m)]
        if not due or (start, min(due)) in self.sent:
            return
        self.sent.update((start, m) for m in due)
        report = await run_blocking(read_preflight, now)
        for part in split_message(report, OPS_MESSAGE_CHARS):
            await sync_to_async(notify_ops)(part)


async def preflight_report_loop() -> None:
    reports = PreflightReports()
    await _every(PREFLIGHT, lambda: PREFLIGHT_SECONDS, reports.run_pass)


# ---------------------------------------------------------------------------------------------------------------
# Session cleanup: deletes expired sessions, which every anonymous visit to the sign-in page adds one of
# ---------------------------------------------------------------------------------------------------------------


async def session_cleanup_loop() -> None:
    await _every(SESSIONS, lambda: SESSION_CLEANUP_MINUTES * 60, session_cleanup_pass)


async def session_cleanup_pass() -> None:
    """Run Django's clearsessions once. A failure raises, so the pass skips its heartbeat: a worker that can't reach
    the database is not alive in any useful sense."""
    await run_blocking(call_command, "clearsessions")
