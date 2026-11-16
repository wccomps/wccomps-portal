"""Sync the competition guild's Discord roles from the stored Authentik groups, which the worker keeps in step with
Authentik (worker.jobs.group_refresh_loop): soon after any stored groups change, and every FULL_SYNC_MINUTES."""

import logging
import time

from asgiref.sync import sync_to_async
from discord.ext import commands, tasks

from bot.role_sync import competition_guild, sync_roles
from core.models import UserGroups

logger = logging.getLogger(__name__)

POLL_SECONDS = 30
FULL_SYNC_MINUTES = 5


def stored_groups() -> int:
    """A hash of every stored account's groups and enabled state, which changes when any of them does. Not
    groups_updated_at: every refresh and sign-in advances it, whether the groups changed or not."""
    rows = UserGroups.objects.order_by("pk").values_list("pk", "account_active", "groups")
    return hash(tuple((pk, active, tuple(groups)) for pk, active, groups in rows))


class RoleSyncCog(commands.Cog):
    """Keeps the Discord roles the stored groups grant in step with them between logins."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # stored_groups() and time.monotonic() at the last sync
        self.synced_groups: int | None = None
        self.synced_at = float("-inf")
        self.sync_task.start()

    async def cog_unload(self) -> None:
        self.sync_task.cancel()

    @tasks.loop(seconds=POLL_SECONDS)
    async def sync_task(self) -> None:
        """Sync the roles if the stored groups changed since the last sync or it was FULL_SYNC_MINUTES ago. Never
        raises, as an exception would end the loop; a sync that fails is tried again at the next change or full sync."""
        try:
            groups = await sync_to_async(stored_groups)()
            now = time.monotonic()
            if groups == self.synced_groups and now - self.synced_at < FULL_SYNC_MINUTES * 60:
                return
            self.synced_groups, self.synced_at = groups, now
            if guild := competition_guild(self.bot):
                await sync_roles(guild)
        except Exception as e:
            logger.warning(f"Role sync failed: {e}")

    @sync_task.before_loop
    async def before_sync(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RoleSyncCog(bot))
