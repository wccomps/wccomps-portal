import logging

import discord
from discord import app_commands
from discord.ext import commands

from bot.permissions import permission_check
from bot.utils import log_to_ops_channel, send_lines
from core.models import AuditLog
from core.utils import role_sync_summary

logger = logging.getLogger(__name__)


class AdminCog(commands.Cog):
    admin_group = app_commands.Group(name="admin", description="General administrative commands")

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @admin_group.command(
        name="sync-roles",
        description="[ADMIN] Preview role synchronization (dry run only)",
    )
    @app_commands.check(permission_check("admin"))
    async def admin_sync_roles(self, interaction: discord.Interaction) -> None:
        """Preview the role sync for the competition guild; the live sync runs every few minutes and from
        the portal Sync Roles page."""
        from bot.role_sync import competition_guild, sync_roles

        await interaction.response.defer(ephemeral=True)

        try:
            guild = competition_guild(self.bot)
            if not guild:
                await interaction.followup.send("Competition guild not found.", ephemeral=True)
                return

            await interaction.followup.send(
                "Starting role synchronization preview (dry run)...\n"
                "No changes will be made. The live sync runs every few minutes, or from the portal's Sync Roles page.",
                ephemeral=True,
            )

            stats = await sync_roles(guild, dry_run=True)
            summary = role_sync_summary(stats, dry_run=True)
            changes_list = stats["changes"]
            await send_lines(
                interaction,
                f"**{summary}**",
                changes_list,
                title="Changes",
                filename="role_sync_preview.txt",
            )

            await AuditLog.objects.acreate(
                action="role_sync",
                admin_user=str(interaction.user),
                target_entity="guilds",
                target_id=0,
                details={
                    "roles_added": stats["roles_added"],
                    "roles_removed": stats["roles_removed"],
                    "errors": stats["errors"],
                    "changes": changes_list[:50],
                },
            )

            await log_to_ops_channel(self.bot, f"Role sync preview by {interaction.user.mention}: {summary}")

        except Exception as e:
            logger.error(f"Role sync failed: {e}", exc_info=True)
            await interaction.followup.send(f"Role sync failed: {e!s}", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AdminCog(bot))
