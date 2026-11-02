import logging

from discord.ext import commands, tasks
from scoring.quotient_sync import sync_quotient_metadata

from bot.utils import run_blocking

logger = logging.getLogger(__name__)

SYNC_MINUTES = 5
# Quotient is routinely off between competitions; check less often and log state changes only
BACKOFF_MINUTES = 15


class QuotientSyncCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.quotient_unavailable = False
        self.sync_quotient_task.start()

    async def cog_unload(self) -> None:
        self.sync_quotient_task.cancel()

    @tasks.loop(minutes=SYNC_MINUTES)
    async def sync_quotient_task(self) -> None:
        """Sync Quotient metadata every SYNC_MINUTES, or BACKOFF_MINUTES while Quotient is unavailable."""
        await self.run_sync()

    async def run_sync(self) -> None:
        """Sync once; back off while Quotient is unavailable."""
        try:
            await run_blocking(sync_quotient_metadata)
        except ValueError as e:
            if self.quotient_unavailable:
                logger.debug(f"Quotient still unavailable: {e}")
            else:
                self.quotient_unavailable = True
                logger.warning(f"Quotient sync failed: {e}; retrying every {BACKOFF_MINUTES} minutes")
                self.sync_quotient_task.change_interval(minutes=BACKOFF_MINUTES)
            return
        except Exception as e:
            logger.exception(f"Error syncing Quotient metadata: {e}")
            return

        if self.quotient_unavailable:
            self.quotient_unavailable = False
            logger.info(f"Quotient reachable again; syncing every {SYNC_MINUTES} minutes")
            self.sync_quotient_task.change_interval(minutes=SYNC_MINUTES)
        logger.debug("Quotient metadata synced")

    @sync_quotient_task.before_loop
    async def before_sync_quotient(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(QuotientSyncCog(bot))
