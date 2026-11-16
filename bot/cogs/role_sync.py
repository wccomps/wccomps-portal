"""Every few minutes: sync the competition guild's Discord roles from the stored Authentik groups, which the worker
keeps in step with Authentik (worker.jobs.group_refresh_loop)."""

import logging

from discord.ext import commands, tasks

from bot.role_sync import competition_guild, sync_roles

logger = logging.getLogger(__name__)

SYNC_MINUTES = 5


async def sync_roles_now(bot: commands.Bot) -> None:
    """Sync the competition guild's roles; never raises (the caller is a background loop)."""
    try:
        if guild := competition_guild(bot):
            await sync_roles(guild)
    except Exception as e:
        logger.warning(f"Role sync failed: {e}")


class RoleSyncCog(commands.Cog):
    """Keeps the Discord roles the stored groups grant in step with them between logins."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.sync_task.start()

    async def cog_unload(self) -> None:
        self.sync_task.cancel()

    @tasks.loop(minutes=SYNC_MINUTES)
    async def sync_task(self) -> None:
        await sync_roles_now(self.bot)

    @sync_task.before_loop
    async def before_sync(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RoleSyncCog(bot))
