"""Competition timer background task to enable/disable applications at scheduled times."""

import contextlib
import logging

from asgiref.sync import sync_to_async
from discord.ext import commands, tasks
from django.utils import timezone

from bot.competition_actions import run_competition, update_status_channel
from bot.heartbeat import beat, beat_for
from bot.utils import log_to_ops_channel, recycle_db_connection, run_blocking
from core.models import CompetitionConfig
from core.services.competition import (
    END_AFTER_STOP,
    TIMER_ACTOR,
    TIMER_ATTEMPTS,
    Attempt,
    GiveUp,
    RunOutcome,
    clear_missed_window,
    current_timer_plan,
    give_up_timer_series,
    queue_due_countdown,
)

logger = logging.getLogger(__name__)


class CompetitionTimer(commands.Cog):
    """Background task to monitor competition start/end times."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.check_loop.start()

    async def cog_unload(self) -> None:
        self.check_loop.cancel()

    @tasks.loop(seconds=60)
    async def check_loop(self) -> None:
        beat_for("timer", self.bot)
        try:
            await recycle_db_connection()
            await self._check_competition_times()
            beat()
        except Exception as e:
            logger.exception(f"Error in competition timer check: {e}")

    @check_loop.before_loop
    async def before_check(self) -> None:
        await self.bot.wait_until_ready()

    async def _check_competition_times(self) -> None:
        """Carry out current_timer_plan. A failure is reported on attempt 1 and when the timer gives up."""
        # Errors here propagate so check_loop skips its heartbeat: a timer that can't read the
        # config is not alive in any useful sense.
        now = timezone.now()
        match await sync_to_async(current_timer_plan)(now):
            case None:
                if await sync_to_async(lambda: CompetitionConfig.get_config().stale_end())():
                    await self._clear_missed_window()
                elif label := await sync_to_async(queue_due_countdown)(now):
                    await log_to_ops_channel(
                        self.bot,
                        f"**Competition Countdown:** queued the {label} remaining announcement for all active teams.",
                    )
            case GiveUp() as plan:
                await self._give_up(plan)
            case Attempt() as attempt:
                await self._attempt(attempt)

    async def _clear_missed_window(self) -> None:
        # Off the shared thread (the lock waits out a Close Out), and caught so the heartbeat keeps beating
        try:
            cleared = await run_blocking(clear_missed_window, TIMER_ACTOR)
        except Exception:
            logger.exception("Could not clear a missed competition window")
            return
        if cleared == END_AFTER_STOP:
            await log_to_ops_channel(self.bot, "**Schedule cleared:** the scheduled end passed after a manual stop.")
            await update_status_channel(self.bot)
        elif cleared:
            await log_to_ops_channel(
                self.bot,
                "**Schedule cleared:** its end passed while the competition was stopped and would have refused "
                "a new start time. Set a new schedule on the Competition setup page.",
            )
            await update_status_channel(self.bot)

    async def _give_up(self, plan: GiveUp) -> None:
        action = "Start" if plan.enable else "Stop"
        if not await sync_to_async(give_up_timer_series)(plan):
            return
        await log_to_ops_channel(
            self.bot,
            f"**Competition Auto-{action} Incomplete:** the timer stopped retrying, as {plan.reason}. "
            f"Run {action} again from the competition page.",
        )
        await update_status_channel(self.bot)

    async def _attempt(self, attempt: Attempt) -> None:
        action, done = ("Start", "Started") if attempt.enable else ("Stop", "Stopped")
        logger.info(f"Competition {action.lower()} attempt {attempt.number} of {TIMER_ATTEMPTS}")
        try:
            result = await run_competition(attempt.enable, actor=TIMER_ACTOR, timer=attempt)
        except Exception as e:
            logger.exception(f"Competition {action.lower()} attempt {attempt.number} failed: {e}")
            if attempt.number == 1:
                with contextlib.suppress(Exception):
                    await log_to_ops_channel(self.bot, f"**Error in competition timer:** {e}")
            return
        match result.outcome:
            case RunOutcome.REFUSED:
                logger.info(f"Competition {action.lower()} attempt skipped: {result.error}")
            case RunOutcome.ERRORED:
                if attempt.number == 1:
                    await log_to_ops_channel(self.bot, f"**Competition Auto-{action} Failed:** {result.error}")
            case RunOutcome.INCOMPLETE:
                if attempt.number == 1:
                    await log_to_ops_channel(
                        self.bot,
                        f"**Competition Auto-{done}, Incomplete:** retrying each minute, up to {TIMER_ATTEMPTS} "
                        f"attempts.\n\n{result.summary()}",
                    )
                    await update_status_channel(self.bot)
            case RunOutcome.COMPLETE:
                message = f"**Competition Auto-{done}!**\n\n{result.summary()}"
                if attempt.number > 1:
                    message += f"\n\nCompleted on attempt {attempt.number}."
                await log_to_ops_channel(self.bot, message)
                await update_status_channel(self.bot)
