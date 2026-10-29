"""Competition timer background task to enable/disable applications at scheduled times."""

import asyncio
import contextlib
import logging

from asgiref.sync import sync_to_async
from discord.ext import commands, tasks

from bot.competition_actions import run_competition, update_status_channel
from bot.heartbeat import beat, beat_for
from bot.utils import log_to_ops_channel, recycle_db_connection
from core.models import CompetitionConfig

logger = logging.getLogger(__name__)

# A start or stop that left some apps or accounts unchanged (Authentik timing out, say) is run again; the
# toggles are idempotent, so a re-run only repeats what already worked.
RETRIES = 3
RETRY_DELAY_SECONDS = 15


class CompetitionTimer(commands.Cog):
    """Background task to monitor competition start/end times."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # Last start/stop failure posted to ops, so a retry that fails the same way stays quiet.
        self._last_failure: str | None = None

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

        # Errors here propagate so check_loop skips its heartbeat: a timer that can't read the
        # config is not alive in any useful sense.
        @sync_to_async
        def check() -> tuple[bool, bool]:
            config = CompetitionConfig.get_config()
            return config.should_enable_applications(), config.should_disable_applications()

        should_start, should_stop = await check()
        if not (should_start or should_stop):
            self._last_failure = None
            return

        try:
            enable = should_start
            action, done = ("Start", "Started") if enable else ("Stop", "Stopped")
            logger.info(f"Competition {action.lower()} time reached")

            result = await run_competition(enable, actor="timer")
            attempts = 1
            superseded = False
            while result.success and result.has_failures and attempts <= RETRIES:
                logger.warning(f"Competition auto-{action.lower()} incomplete; retrying ({attempts}/{RETRIES})")
                await asyncio.sleep(RETRY_DELAY_SECONDS)
                # Someone may have started or stopped it by hand meanwhile; don't undo that
                if await sync_to_async(lambda: CompetitionConfig.get_config().applications_enabled)() != enable:
                    superseded = True
                    break
                result = await run_competition(enable, actor="timer")
                attempts += 1
            if result.success:
                result_msg = f"**Competition Auto-{done}!**\n\n{result.summary()}"
                if superseded:
                    changed = "stopped" if enable else "started"
                    result_msg += f"\n\nIncomplete, and retries stopped: the competition was {changed} by hand."
                elif result.has_failures:
                    result_msg += (
                        f"\n\nStill incomplete after {attempts} attempts: run {action} again from the competition page."
                    )
                elif attempts > 1:
                    result_msg += f"\n\nCompleted on attempt {attempts}."
            else:
                result_msg = f"**Competition Auto-{action} Failed:** {result.error}"
                # A run that errors leaves the state unchanged, so the next check runs it again; report it once.
                if result_msg == self._last_failure:
                    return
                self._last_failure = result_msg

            await log_to_ops_channel(self.bot, result_msg)
            await update_status_channel(self.bot)

        except Exception as e:
            logger.exception(f"Failed to start/stop competition: {e}")
            message = f"**Error in competition timer:** {e}"
            if message != self._last_failure:
                self._last_failure = message
                with contextlib.suppress(Exception):
                    await log_to_ops_channel(self.bot, message)
