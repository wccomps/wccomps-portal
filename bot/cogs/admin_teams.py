"""Admin commands for team management."""

import logging
import re

import discord
from asgiref.sync import sync_to_async
from discord import app_commands
from discord.ext import commands
from django.utils import timezone

from bot.discord_manager import delete_team_infrastructure, unlink_team_members
from bot.permissions import permission_check
from bot.role_sync import sync_member_roles
from bot.utils import (
    DISCORD_EMBED_DESCRIPTION_CHAR_LIMIT,
    DISCORD_EMBED_FIELD_CHAR_LIMIT,
    add_field,
    fit_lines,
    get_team_or_respond,
    log_to_ops_channel,
    send_lines,
)
from core.authentik_utils import parse_team_range, reset_team_credentials
from core.models import AuditLog, CompetitionConfig
from core.utils import shorten
from team.models import MAX_TEAMS, DiscordLink, Team

logger = logging.getLogger(__name__)


class AdminTeamsCog(commands.Cog):
    """Admin commands for team management."""

    teams_group = app_commands.Group(name="teams", description="Team management commands")

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @teams_group.command(name="list", description="[ADMIN] List all teams with status")
    @app_commands.check(permission_check("admin"))
    async def admin_teams(self, interaction: discord.Interaction) -> None:
        """List all teams with member counts."""
        teams = [team async for team in Team.objects.all().order_by("team_number")]
        max_members = (await sync_to_async(CompetitionConfig.get_config)()).max_team_members
        team_statuses = []

        for team in teams:
            member_count = await team.members.filter(is_active=True).acount()
            status = f"#{team.team_number:02d} {team.team_name}: {member_count}/{max_members} members"
            team_statuses.append(status)

        embed = discord.Embed(
            title=f"Team Status ({len(team_statuses)} teams)",
            description=fit_lines(team_statuses, DISCORD_EMBED_DESCRIPTION_CHAR_LIMIT) or "No teams",
            color=discord.Color.blue(),
        )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @teams_group.command(name="info", description="[ADMIN] Get detailed info about a specific team")
    @app_commands.describe(team_number="Team number (1-50)")
    @app_commands.check(permission_check("admin"))
    async def admin_team_info(self, interaction: discord.Interaction, team_number: int) -> None:
        """Get detailed information about a team."""
        team = await get_team_or_respond(interaction, team_number)
        if not team:
            return

        member_count = await team.members.filter(is_active=True).acount()
        max_members = (await sync_to_async(CompetitionConfig.get_config)()).max_team_members
        members = [m async for m in team.members.filter(is_active=True).select_related("user").order_by("linked_at")]

        embed = discord.Embed(title=f"{team.team_name} Details", color=discord.Color.blue())
        embed.add_field(name="Team Number", value=f"#{team.team_number}", inline=True)
        embed.add_field(name="Members", value=f"{member_count}/{max_members}", inline=True)
        add_field(embed, "Authentik Group", team.authentik_group or "(none)")

        if members:
            member_lines = [
                f"• {shorten(m.discord_username, 40)} (ID: {m.discord_id}), via `{shorten(m.user.username, 40)}`"
                for m in members
            ]
            add_field(embed, "Team Members", fit_lines(member_lines, DISCORD_EMBED_FIELD_CHAR_LIMIT))

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @teams_group.command(
        name="unlink",
        description="[ADMIN] Unlink one or more Discord users from their teams",
    )
    @app_commands.describe(users="User mention(s), ID(s), or space-separated list of multiple users")
    @app_commands.check(permission_check("admin"))
    async def admin_unlink(self, interaction: discord.Interaction, users: str) -> None:
        """Unlink one or more Discord users from their teams."""
        await interaction.response.defer(ephemeral=True)

        if not interaction.guild:
            await interaction.followup.send("This command must be used in a guild", ephemeral=True)
            return

        user_ids = []
        # Match Discord user mentions <@123456789> or <@!123456789> or raw IDs
        mention_pattern = r"<@!?(\d+)>|\b(\d{17,20})\b"
        matches = re.findall(mention_pattern, users)
        for match in matches:
            # match is a tuple of groups, get the non-empty one
            user_id = int(match[0] if match[0] else match[1])
            user_ids.append(user_id)

        if not user_ids:
            await interaction.followup.send(
                "No valid user mentions or IDs found. Please provide user mention(s) or ID(s).",
                ephemeral=True,
            )
            return

        user_ids = list(set(user_ids))

        results = []
        success_count = 0
        error_count = 0

        for user_id in user_ids:
            try:
                member = interaction.guild.get_member(user_id)
                user_display = member.mention if member else f"User ID {user_id}"

                link = await (
                    DiscordLink.objects.filter(discord_id=user_id, is_active=True)
                    .select_related("team", "user")
                    .afirst()
                )

                if not link:
                    results.append(f"{user_display}: not linked to any team")
                    error_count += 1
                    continue

                team = link.team
                if not team:
                    results.append(f"{user_display}: the link has no team")
                    error_count += 1
                    continue

                link.is_active = False
                link.unlinked_at = timezone.now()
                await link.asave()

                if member:
                    await sync_member_roles(interaction.guild, member)

                await AuditLog.objects.acreate(
                    action="user_unlinked",
                    admin_user=str(interaction.user),
                    target_entity="discord_link",
                    target_id=user_id,
                    details={
                        "discord_username": str(member) if member else f"ID:{user_id}",
                        "team_name": team.team_name,
                        "authentik_username": link.user.username,
                        "multiple_users": len(user_ids) > 1,
                    },
                )

                results.append(f"{user_display}: unlinked from **{team.team_name}**")
                success_count += 1

            except Exception as e:
                logger.exception(f"Error unlinking user {user_id}: {e}")
                results.append(f"User ID {user_id}: failed ({shorten(str(e), 50)})")
                error_count += 1

        if len(user_ids) == 1:
            await interaction.followup.send(results[0], ephemeral=True)
            if success_count > 0:
                await log_to_ops_channel(
                    self.bot,
                    f"User Unlinked: {results[0].split(':')[0]} by {interaction.user.mention}",
                )
        else:
            summary = "**Unlink Results**\n\n"
            summary += f"Unlinked: {success_count}\n"
            if error_count > 0:
                summary += f"Failed: {error_count}\n"
            summary += f"Total processed: {len(user_ids)}\n\n"

            await send_lines(interaction, summary.rstrip(), results, title="Details", filename="results.txt")
            await log_to_ops_channel(
                self.bot,
                f"Bulk Unlink: {interaction.user.mention} unlinked {success_count} user(s) "
                f"(Processed: {len(user_ids)}, Failed: {error_count})",
            )

    @teams_group.command(
        name="remove",
        description="[ADMIN] Remove team infrastructure and unlink all members",
    )
    @app_commands.describe(team_number="Team number (1-50)")
    @app_commands.check(permission_check("admin"))
    async def admin_remove_team(self, interaction: discord.Interaction, team_number: int) -> None:
        """Remove a team's Discord infrastructure and unlink all members."""
        if team_number < 1 or team_number > MAX_TEAMS:
            await interaction.response.send_message(f"Team number must be between 1 and {MAX_TEAMS}", ephemeral=True)
            return

        guild = interaction.guild
        if not guild:
            await interaction.response.send_message("This command must be used in a guild", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        team = await Team.objects.filter(team_number=team_number).afirst()
        if not team:
            await interaction.followup.send(f"Team {team_number} not found", ephemeral=True)
            return

        unlinked_count = await unlink_team_members(
            guild,
            team,
            str(interaction.user),
            audit_reason="team_removed",
        )

        removed_items = await delete_team_infrastructure(
            guild, team, reason=f"Team {team_number} removed by {interaction.user}"
        )

        await AuditLog.objects.acreate(
            action="team_removed",
            admin_user=str(interaction.user),
            target_entity="team",
            target_id=team_number,
            details={
                "team_name": team.team_name,
                "unlinked_members": unlinked_count,
                "removed_items": removed_items,
            },
        )

        await log_to_ops_channel(
            self.bot,
            f"Team Removed: **{team.team_name}** by {interaction.user.mention}\n"
            f"• Unlinked {unlinked_count} members\n"
            f"• Removed {', '.join(removed_items) if removed_items else 'nothing'}",
        )

        await interaction.followup.send(
            f"Removed **{team.team_name}**\n"
            f"• Unlinked {unlinked_count} members\n"
            f"• Removed {', '.join(removed_items) if removed_items else 'nothing'}",
            ephemeral=True,
        )

    @teams_group.command(
        name="reset",
        description="[ADMIN] Team reset: unlink users, reset password, revoke sessions, recreate channels",
    )
    @app_commands.describe(
        team_number="Team number (1-50)",
        recreate_channels="Whether to recreate Discord channels and role (default: True)",
    )
    @app_commands.check(permission_check("admin"))
    async def admin_reset_team(
        self,
        interaction: discord.Interaction,
        team_number: int,
        recreate_channels: bool = True,
    ) -> None:
        """Team reset: unlinks users, resets password, revokes sessions, and optionally recreates infrastructure."""
        if team_number < 1 or team_number > MAX_TEAMS:
            await interaction.response.send_message(f"Team number must be between 1 and {MAX_TEAMS}", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        if not interaction.guild:
            await interaction.followup.send("This command must be used in a guild", ephemeral=True)
            return

        team = await Team.objects.filter(team_number=team_number).afirst()
        if not team:
            await interaction.followup.send(f"Team {team_number} not found", ephemeral=True)
            return

        results = []
        guild = interaction.guild

        unlinked_count = await unlink_team_members(
            guild,
            team,
            str(interaction.user),
            audit_reason="team_reset",
        )

        results.append(f"Unlinked {unlinked_count} Discord user(s)")

        new_password, error, sessions_revoked = await sync_to_async(reset_team_credentials)(team_number)
        if new_password:
            results.append("Reset the Authentik password")
            if sessions_revoked:
                results.append("Revoked active session(s)")
            else:
                results.append("Failed to revoke sessions")
        else:
            results.append(f"Failed to reset the password: {error}")

        if recreate_channels:
            deleted_items = await delete_team_infrastructure(
                guild, team, reason=f"Team {team_number} reset by {interaction.user}"
            )

            if deleted_items:
                results.append(f"Deleted {', '.join(deleted_items)}")

            from bot.discord_manager import DiscordManager

            discord_manager = DiscordManager(guild, self.bot)
            role, category = await discord_manager.setup_team_infrastructure(team_number)

            if role and category:
                results.append(f"Recreated the role and category with {len(category.channels)} channels")
            elif role:
                results.append("Recreated the role (creating the category failed)")
            elif category:
                results.append("Recreated the category (creating the role failed)")
            else:
                results.append("Failed to recreate the role and category")
        else:
            results.append("Skipped recreating channels (recreate_channels=False)")

        await AuditLog.objects.acreate(
            action="team_reset",
            admin_user=str(interaction.user),
            target_entity="team",
            target_id=team_number,
            details={
                "team_name": team.team_name,
                "unlinked_members": unlinked_count,
                "password_reset": new_password is not None,
                "sessions_revoked": sessions_revoked,
                "recreated_channels": recreate_channels,
            },
        )

        await log_to_ops_channel(
            self.bot,
            f"Team Reset: **{team.team_name}** by {interaction.user.mention}\n"
            + "\n".join([f"• {r}" for r in results]),
        )

        response = f"**Team {team_number} Reset Complete**\n\n" + "\n".join(results)
        if new_password:
            response += f"\n\n**New Team Password:** `{new_password}`"
            response += "\n\nMake sure to securely share this password with the team."
        await interaction.followup.send(response, ephemeral=True)

    @teams_group.command(
        name="activate",
        description="[ADMIN] Activate multiple teams in database",
    )
    @app_commands.describe(teams="Team numbers (e.g., '1,3,5-10,15')")
    @app_commands.check(permission_check("admin"))
    async def admin_activate_teams(self, interaction: discord.Interaction, teams: str) -> None:
        """Activate multiple teams in database (sets is_active=True)."""
        await interaction.response.defer(ephemeral=True)

        try:
            team_numbers = parse_team_range(teams)
        except ValueError as e:
            await interaction.followup.send(f"Error: {e}", ephemeral=True)
            return

        if not team_numbers:
            await interaction.followup.send("No teams specified", ephemeral=True)
            return

        results = []
        success_count = 0
        for team_number in team_numbers:
            team = await Team.objects.filter(team_number=team_number).afirst()
            if not team:
                results.append(f"Team {team_number:02d}: not found")
                continue

            if team.is_active:
                results.append(f"Team {team_number:02d}: already active")
                continue

            team.is_active = True
            await team.asave(update_fields=["is_active"])
            if interaction.guild:
                async for link in DiscordLink.objects.filter(team=team, is_active=True):
                    member = interaction.guild.get_member(link.discord_id)
                    if member:
                        await sync_member_roles(interaction.guild, member)
            results.append(f"Team {team_number:02d}: activated")
            success_count += 1

        await AuditLog.objects.acreate(
            action="teams_activated",
            admin_user=str(interaction.user),
            target_entity="teams",
            target_id=0,
            details={
                "team_numbers": team_numbers,
                "success_count": success_count,
                "total_count": len(team_numbers),
            },
        )

        await log_to_ops_channel(
            self.bot,
            f"Team Activation by {interaction.user.mention}\n"
            f"• Teams: {teams}\n"
            f"• Activated: {success_count}/{len(team_numbers)}",
        )

        summary = "**Team Activation Results**\n\n"
        summary += f"Activated: {success_count}/{len(team_numbers)}\n\n"

        await send_lines(interaction, summary.rstrip(), results, title="Details", filename="results.txt")

    @teams_group.command(
        name="deactivate",
        description="[ADMIN] Deactivate multiple teams in database",
    )
    @app_commands.describe(teams="Team numbers (e.g., '1,3,5-10,15')")
    @app_commands.check(permission_check("admin"))
    async def admin_deactivate_teams(self, interaction: discord.Interaction, teams: str) -> None:
        """Deactivate multiple teams in database (sets is_active=False)."""
        await interaction.response.defer(ephemeral=True)

        try:
            team_numbers = parse_team_range(teams)
        except ValueError as e:
            await interaction.followup.send(f"Error: {e}", ephemeral=True)
            return

        if not team_numbers:
            await interaction.followup.send("No teams specified", ephemeral=True)
            return

        results = []
        success_count = 0
        for team_number in team_numbers:
            team = await Team.objects.filter(team_number=team_number).afirst()
            if not team:
                results.append(f"Team {team_number:02d}: not found")
                continue

            if not team.is_active:
                results.append(f"Team {team_number:02d}: already inactive")
                continue

            team.is_active = False
            await team.asave(update_fields=["is_active"])
            if interaction.guild:
                async for link in DiscordLink.objects.filter(team=team, is_active=True):
                    member = interaction.guild.get_member(link.discord_id)
                    if member:
                        await sync_member_roles(interaction.guild, member)
            results.append(f"Team {team_number:02d}: deactivated")
            success_count += 1

        await AuditLog.objects.acreate(
            action="teams_deactivated",
            admin_user=str(interaction.user),
            target_entity="teams",
            target_id=0,
            details={
                "team_numbers": team_numbers,
                "success_count": success_count,
                "total_count": len(team_numbers),
            },
        )

        await log_to_ops_channel(
            self.bot,
            f"Team Deactivation by {interaction.user.mention}\n"
            f"• Teams: {teams}\n"
            f"• Deactivated: {success_count}/{len(team_numbers)}",
        )

        summary = "**Team Deactivation Results**\n\n"
        summary += f"Deactivated: {success_count}/{len(team_numbers)}\n"
        summary += "Their members lose their team roles within a few minutes; reactivating restores them.\n\n"

        await send_lines(interaction, summary.rstrip(), results, title="Details", filename="results.txt")

    @teams_group.command(
        name="recreate",
        description="[ADMIN] Recreate Discord infrastructure for multiple teams",
    )
    @app_commands.describe(teams="Team numbers (e.g., '1,3,5-10,15')")
    @app_commands.check(permission_check("admin"))
    async def admin_recreate_teams(self, interaction: discord.Interaction, teams: str) -> None:
        """Recreate Discord infrastructure (roles, channels) for multiple teams."""
        await interaction.response.defer(ephemeral=True)

        if not interaction.guild:
            await interaction.followup.send("This command must be used in a guild", ephemeral=True)
            return

        try:
            team_numbers = parse_team_range(teams)
        except ValueError as e:
            await interaction.followup.send(f"Error: {e}", ephemeral=True)
            return

        if not team_numbers:
            await interaction.followup.send("No teams specified", ephemeral=True)
            return

        from bot.discord_manager import DiscordManager

        discord_manager = DiscordManager(interaction.guild, self.bot)
        guild = interaction.guild

        results = []
        success_count = 0
        failed_count = 0

        for team_number in team_numbers:
            team = await Team.objects.filter(team_number=team_number).afirst()
            if not team:
                results.append(f"Team {team_number:02d}: not found")
                failed_count += 1
                continue

            await delete_team_infrastructure(guild, team, reason=f"Bulk recreate by {interaction.user}")

            role, category = await discord_manager.setup_team_infrastructure(team_number)

            if role and category:
                results.append(f"Team {team_number:02d}: recreated (role and {len(category.channels)} channels)")
                success_count += 1
            elif role or category:
                results.append(f"Team {team_number:02d}: partly recreated")
                failed_count += 1
            else:
                results.append(f"Team {team_number:02d}: failed to recreate")
                failed_count += 1

        await AuditLog.objects.acreate(
            action="teams_recreated",
            admin_user=str(interaction.user),
            target_entity="teams",
            target_id=0,
            details={
                "team_numbers": team_numbers,
                "success_count": success_count,
                "failed_count": failed_count,
                "total_count": len(team_numbers),
            },
        )

        await log_to_ops_channel(
            self.bot,
            f"Team Infrastructure Recreate by {interaction.user.mention}\n"
            f"• Teams: {teams}\n"
            f"• Success: {success_count}\n"
            f"• Failed: {failed_count}",
        )

        summary = "**Team Recreation Results**\n\n"
        summary += f"Recreated: {success_count}/{len(team_numbers)}\n"
        if failed_count > 0:
            summary += f"Failed: {failed_count}/{len(team_numbers)}\n"
        summary += "\n"

        await send_lines(interaction, summary.rstrip(), results, title="Details", filename="results.txt")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AdminTeamsCog(bot))
