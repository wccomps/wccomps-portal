"""Admin commands for competition and account management."""

import io
import logging

import discord
from asgiref.sync import sync_to_async
from discord import app_commands
from discord.ext import commands
from django.conf import settings

from bot.competition_actions import run_competition_cleanup
from bot.permissions import permission_check
from bot.utils import ConfirmView, log_to_ops_channel, team_chat_channel
from core.authentik_utils import parse_team_range, reset_team_credentials
from core.csv_export import csv_writer
from core.models import AuditLog, CompetitionConfig, QueuedAnnouncement
from core.utils import UnknownTimezoneError, parse_datetime_to_utc
from team.models import Team, active_team_numbers, team_username

logger = logging.getLogger(__name__)


class AdminCompetitionCog(commands.Cog):
    """Admin commands for competition and account management."""

    competition_group = app_commands.Group(
        name="competition",
        description="Competition management commands",
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @competition_group.command(
        name="reset-blueteam-passwords",
        description="Reset passwords for blueteam accounts (optionally specify teams)",
    )
    @app_commands.check(permission_check("gold_team"))
    async def admin_reset_blueteam_passwords(
        self, interaction: discord.Interaction, team_numbers: str | None = None
    ) -> None:
        """Reset passwords for blueteam accounts and export CSV.

        Args:
            team_numbers: Optional comma-separated team numbers or ranges (e.g., "1,3,5-10")
                         If not provided, resets every active team's account.
        """

        if not settings.AUTHENTIK_TOKEN:
            await interaction.response.send_message("Error: AUTHENTIK_TOKEN not configured in settings", ephemeral=True)
            return

        if not team_numbers:
            active = await sync_to_async(active_team_numbers)()
            view = ConfirmView(confirm_label=f"Confirm Reset All {len(active)} Active Teams")
            await interaction.response.send_message(
                f"⚠️ **WARNING: You are about to reset passwords for ALL {len(active)} active team accounts.**\n\n"
                "This will:\n"
                "• Generate new random passwords for them\n"
                "• Invalidate all current passwords and end their Authentik sessions\n\n"
                "Are you sure you want to continue?",
                view=view,
                ephemeral=True,
            )

            await view.wait()

            if not view.confirmed:
                return

            await interaction.followup.send(f"Resetting all {len(active)} active team passwords...", ephemeral=True)
        else:
            await interaction.response.defer(ephemeral=True)

        if team_numbers:
            try:
                teams = parse_team_range(team_numbers)
            except ValueError as e:
                await interaction.followup.send(f"Error: {e}", ephemeral=True)
                return
        else:
            teams = active

        # Only successful resets go in the CSV: a failed one leaves the old password in place.
        password_list = []
        failed_resets = []
        sessions_failed = []
        for team_num in teams:
            username = team_username(team_num)
            password, error, sessions_revoked = await sync_to_async(reset_team_credentials)(team_num)
            if password:
                password_list.append((team_num, username, password))
                if not sessions_revoked:
                    sessions_failed.append(username)
            else:
                failed_resets.append((username, error))

        total = len(teams)
        await AuditLog.objects.acreate(
            action="blueteam_passwords_reset",
            admin_user=str(interaction.user),
            target_entity="authentik_users",
            target_id=0,
            details={
                "total_users": total,
                "success_count": len(password_list),
                "failed_resets": len(failed_resets),
                "sessions_failed": sessions_failed,
                "team_numbers": team_numbers or "all active",
            },
        )

        teams_msg = f"teams {team_numbers}" if team_numbers else f"all {total} active team accounts"
        await log_to_ops_channel(
            self.bot,
            f"BlueTeam Password Reset by {interaction.user.mention}\n"
            f"• Teams: {teams_msg}\n"
            f"• Reset: {len(password_list)}/{total}\n"
            f"• Failed: {len(failed_resets)}",
        )

        result_msg = f"Password reset complete\n• Success: {len(password_list)}/{total}\n"
        result_msg += "".join(f"• Failed {username}: {error}\n" for username, error in failed_resets)
        if sessions_failed:
            result_msg += f"• Could not revoke sessions for {', '.join(sessions_failed)}\n"
        if not password_list:
            await interaction.followup.send(result_msg, ephemeral=True)
            return

        csv_buffer = io.StringIO()
        writer = csv_writer(csv_buffer)
        writer.writerow(["Username", "Password"])
        writer.writerows((username, password) for _num, username, password in password_list)
        file = discord.File(fp=io.BytesIO(csv_buffer.getvalue().encode("utf-8")), filename="blueteam_passwords.csv")
        await interaction.followup.send(
            result_msg + "\nCSV attached with the new credentials.", file=file, ephemeral=True
        )

        logger.info(f"Password reset performed by {interaction.user}. Failed: {len(failed_resets)}")

    @competition_group.command(name="set-max-members", description="[ADMIN] Set maximum team members globally")
    @app_commands.describe(max_members="Maximum members per team (1-20)")
    @app_commands.check(permission_check("admin"))
    async def admin_set_max_members(self, interaction: discord.Interaction, max_members: int) -> None:
        """Set global maximum team members."""
        if max_members < 1 or max_members > 20:
            await interaction.response.send_message("Maximum members must be between 1 and 20.", ephemeral=True)
            return

        config = await sync_to_async(CompetitionConfig.get_config)()
        old_max = config.max_team_members
        config.max_team_members = max_members
        await config.asave(update_fields=["max_team_members"])

        await AuditLog.objects.acreate(
            action="max_team_members_updated",
            admin_user=str(interaction.user),
            target_entity="competition_config",
            target_id=config.pk,
            details={
                "old_max": old_max,
                "new_max": max_members,
            },
        )

        await log_to_ops_channel(
            self.bot,
            f"Max Team Members Updated by {interaction.user.mention}\n• Old: {old_max}\n• New: {max_members}",
        )

        await interaction.response.send_message(
            f"Maximum team members set to {max_members} (was {old_max}).",
            ephemeral=True,
        )

    @competition_group.command(
        name="set-start-time",
        description="[ADMIN] Set competition start time (applications will be enabled automatically)",
    )
    @app_commands.describe(
        datetime_str="Start time in format: YYYY-MM-DDTHH:MM (e.g., 2025-01-15T09:00)",
        timezone_name="Timezone (defaults to Pacific Time)",
    )
    @app_commands.choices(
        timezone_name=[
            app_commands.Choice(name="Pacific Time (PT)", value="America/Los_Angeles"),
            app_commands.Choice(name="Mountain Time (MT)", value="America/Denver"),
            app_commands.Choice(name="Central Time (CT)", value="America/Chicago"),
            app_commands.Choice(name="Eastern Time (ET)", value="America/New_York"),
            app_commands.Choice(name="UTC", value="UTC"),
        ]
    )
    @app_commands.check(permission_check("admin"))
    async def admin_set_start_time(
        self,
        interaction: discord.Interaction,
        datetime_str: str,
        timezone_name: str = "America/Los_Angeles",
    ) -> None:
        """Set competition start time for automatic application enabling."""
        try:
            start_time = parse_datetime_to_utc(datetime_str, timezone_name)
        except UnknownTimezoneError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        except ValueError:
            await interaction.response.send_message(
                "Invalid datetime format. Use: YYYY-MM-DDTHH:MM (e.g., 2025-01-15T09:00)",
                ephemeral=True,
            )
            return

        config = await sync_to_async(CompetitionConfig.get_config)()
        if error := config.schedule_error(start=start_time):
            await interaction.response.send_message(error, ephemeral=True)
            return

        config.competition_start_time = start_time
        await config.asave(update_fields=["competition_start_time"])

        await AuditLog.objects.acreate(
            action="competition_start_time_set",
            admin_user=str(interaction.user),
            target_entity="competition_config",
            target_id=config.pk,
            details={
                "start_time": start_time.isoformat(),
                "controlled_apps": config.controlled_applications,
            },
        )

        await log_to_ops_channel(
            self.bot,
            f"Competition Start Time Set by {interaction.user.mention}\n"
            f"• Start Time: {discord.utils.format_dt(start_time, style='F')}\n"
            f"• Controlled Apps: {', '.join(config.controlled_applications)}\n"
            f"• Applications will be enabled automatically at start time",
        )

        await interaction.response.send_message(
            f"Competition start time set to: {discord.utils.format_dt(start_time, style='F')}\n"
            f"Controlled applications: {', '.join(config.controlled_applications)}\n\n"
            f"Applications will be automatically enabled at start time.",
            ephemeral=True,
        )

    @competition_group.command(
        name="set-end-time",
        description="[ADMIN] Set competition end time (applications will be disabled automatically)",
    )
    @app_commands.describe(
        datetime_str="End time in format: YYYY-MM-DDTHH:MM (e.g., 2025-01-15T17:00)",
        timezone_name="Timezone (defaults to Pacific Time)",
    )
    @app_commands.choices(
        timezone_name=[
            app_commands.Choice(name="Pacific Time (PT)", value="America/Los_Angeles"),
            app_commands.Choice(name="Mountain Time (MT)", value="America/Denver"),
            app_commands.Choice(name="Central Time (CT)", value="America/Chicago"),
            app_commands.Choice(name="Eastern Time (ET)", value="America/New_York"),
            app_commands.Choice(name="UTC", value="UTC"),
        ]
    )
    @app_commands.check(permission_check("admin"))
    async def admin_set_end_time(
        self,
        interaction: discord.Interaction,
        datetime_str: str,
        timezone_name: str = "America/Los_Angeles",
    ) -> None:
        """Set competition end time for automatic application disabling."""
        try:
            end_time = parse_datetime_to_utc(datetime_str, timezone_name)
        except UnknownTimezoneError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        except ValueError:
            await interaction.response.send_message(
                "Invalid datetime format. Use: YYYY-MM-DDTHH:MM (e.g., 2025-01-15T17:00)",
                ephemeral=True,
            )
            return

        config = await sync_to_async(CompetitionConfig.get_config)()
        if error := config.schedule_error(end=end_time):
            await interaction.response.send_message(error, ephemeral=True)
            return

        config.competition_end_time = end_time
        await config.asave(update_fields=["competition_end_time"])

        await AuditLog.objects.acreate(
            action="competition_end_time_set",
            admin_user=str(interaction.user),
            target_entity="competition_config",
            target_id=config.pk,
            details={
                "end_time": end_time.isoformat(),
                "controlled_apps": config.controlled_applications,
            },
        )

        await log_to_ops_channel(
            self.bot,
            f"Competition End Time Set by {interaction.user.mention}\n"
            f"• End Time: {discord.utils.format_dt(end_time, style='F')}\n"
            f"• Controlled Apps: {', '.join(config.controlled_applications)}\n"
            f"• Applications will be disabled automatically at end time",
        )

        await interaction.response.send_message(
            f"Competition end time set to: {discord.utils.format_dt(end_time, style='F')}\n"
            f"Controlled applications: {', '.join(config.controlled_applications)}\n\n"
            f"Applications will be automatically disabled at end time.",
            ephemeral=True,
        )

    @competition_group.command(
        name="start-competition",
        description="[ADMIN] Start the competition (enable applications and accounts)",
    )
    @app_commands.check(permission_check("admin"))
    async def admin_start_competition(self, interaction: discord.Interaction) -> None:
        """Start the competition by enabling applications and Authentik accounts."""
        await self._run_competition(interaction, enable=True)

    @competition_group.command(
        name="stop-competition",
        description="[ADMIN] Stop the competition (disable applications and accounts)",
    )
    @app_commands.check(permission_check("admin"))
    async def admin_stop_competition(self, interaction: discord.Interaction) -> None:
        """Stop the competition by disabling applications and Authentik accounts."""
        await self._run_competition(interaction, enable=False)

    async def _run_competition(self, interaction: discord.Interaction, enable: bool) -> None:
        from bot.competition_actions import run_competition, update_status_channel

        await interaction.response.defer(ephemeral=True)
        result = await run_competition(enable, actor=f"discord:{interaction.user}")
        if not result.success:
            await interaction.followup.send(f"Error: {result.error}", ephemeral=True)
            return

        verb = "Started" if enable else "Stopped"
        incomplete = " (incomplete)" if result.has_failures else ""
        await log_to_ops_channel(
            self.bot, f"**Competition {verb}{incomplete}** by {interaction.user.mention}\n{result.summary()}"
        )
        await update_status_channel(self.bot)
        note = f"\n\nIncomplete: run {'start' if enable else 'stop'}-competition again." if result.has_failures else ""
        await interaction.followup.send(
            f"**Competition {verb}{incomplete}!**\n\n{result.summary()}{note}", ephemeral=True
        )

    @competition_group.command(
        name="cleanup-competition",
        description="[ADMIN] Clean up Discord infrastructure (requires competition stopped)",
    )
    @app_commands.check(permission_check("admin"))
    async def admin_cleanup_competition(self, interaction: discord.Interaction) -> None:
        """Clean up Discord channels, roles, and links after competition."""

        config = await sync_to_async(CompetitionConfig.get_config)()
        if config.applications_enabled:
            await interaction.response.send_message(
                "Competition must be stopped before cleanup. Use `/competition stop-competition` first.",
                ephemeral=True,
            )
            return

        view = ConfirmView(confirm_label="Confirm Cleanup")
        await interaction.response.send_message(
            "**This will permanently:**\n"
            "- Delete all team Discord channels and categories\n"
            "- Remove team roles from all members\n"
            "- Deactivate all team member Discord links\n"
            "- Remove the Room Judge role\n"
            "- Clear queued announcements\n\n"
            "Are you sure you want to continue?",
            view=view,
            ephemeral=True,
        )

        await view.wait()
        if not view.confirmed:
            await interaction.followup.send("Cleanup cancelled.", ephemeral=True)
            return

        guild = interaction.guild
        if not guild:
            await interaction.followup.send("This command must be used in a guild", ephemeral=True)
            return
        await interaction.followup.send(
            "Starting cleanup... This runs in background. Check ops channel for progress.",
            ephemeral=True,
        )
        self.bot.loop.create_task(run_competition_cleanup(self.bot, guild, str(interaction.user)))

    @competition_group.command(
        name="set-apps",
        description="[ADMIN] Set which Authentik applications to control",
    )
    @app_commands.describe(app_slugs="Comma-separated list of application slugs (e.g., netbird,scoring)")
    @app_commands.check(permission_check("admin"))
    async def admin_competition_set_apps(self, interaction: discord.Interaction, app_slugs: str) -> None:
        """Set which applications to control."""

        slugs = [s.strip() for s in app_slugs.split(",") if s.strip()]

        if not slugs:
            await interaction.response.send_message("Please provide at least one application slug.", ephemeral=True)
            return

        config = await sync_to_async(CompetitionConfig.get_config)()
        config.controlled_applications = slugs
        await config.asave()

        await AuditLog.objects.acreate(
            action="competition_apps_configured",
            admin_user=str(interaction.user),
            target_entity="competition_config",
            target_id=config.pk,
            details={
                "controlled_apps": slugs,
            },
        )

        await log_to_ops_channel(
            self.bot,
            f"Competition Applications Configured by {interaction.user.mention}\n• Applications: {', '.join(slugs)}",
        )

        await interaction.response.send_message(f"Controlled applications set to: {', '.join(slugs)}", ephemeral=True)

    @competition_group.command(
        name="broadcast",
        description="[ADMIN] Broadcast a message to announcement channel or team channels",
    )
    @app_commands.describe(
        target="Where to broadcast: 'announcements', 'all-teams', or specific teams (e.g., '1,3,5-10')",
        message="Message to broadcast",
    )
    @app_commands.check(permission_check("admin"))
    async def admin_broadcast(self, interaction: discord.Interaction, target: str, message: str) -> None:
        """Broadcast a message to announcement channel or team channels."""

        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        if not guild:
            await interaction.followup.send("This command must be used in a guild", ephemeral=True)
            return

        target_lower = target.lower().strip()
        sent_count = 0
        failed_channels = []

        if target_lower == "announcements":
            announcement_channel_id = settings.DISCORD_ANNOUNCEMENT_CHANNEL_ID
            channel = guild.get_channel(announcement_channel_id)

            if not channel or not isinstance(channel, discord.TextChannel):
                await interaction.followup.send(
                    f"Announcements channel not found (ID: {announcement_channel_id})",
                    ephemeral=True,
                )
                return

            blueteam_role = guild.get_role(settings.BLUETEAM_ROLE_ID)
            role_mention = blueteam_role.mention if blueteam_role else "@Blueteam"

            try:
                await channel.send(f"{role_mention}\n\n{message}")
                sent_count = 1

                await log_to_ops_channel(
                    self.bot,
                    f"Broadcast to Announcements by {interaction.user.mention}\nMessage: {message[:100]}...",
                )

                await interaction.followup.send("Broadcast sent to announcements channel", ephemeral=True)
            except Exception as e:
                logger.exception(f"Failed to broadcast to announcements: {e}")
                await interaction.followup.send(f"Failed to send broadcast: {e}", ephemeral=True)
            return

        if target_lower == "all-teams":
            teams = [t async for t in Team.objects.filter(is_active=True).order_by("team_number")]
            team_numbers = [t.team_number for t in teams]

        else:
            try:
                team_numbers = parse_team_range(target)
            except ValueError as e:
                await interaction.followup.send(
                    f"Invalid team range format: {e}\n\n"
                    f"Examples:\n"
                    f"• Single teams: `1,3,5`\n"
                    f"• Range: `1-10`\n"
                    f"• Mixed: `1,3,5-10,15`\n"
                    f"• All teams: `all-teams`\n"
                    f"• Announcements: `announcements`",
                    ephemeral=True,
                )
                return

        queued_count = 0

        for team_number in team_numbers:
            try:
                team = await Team.objects.filter(team_number=team_number).afirst()
                if not team:
                    failed_channels.append(f"Team {team_number:02d} (not found)")
                    continue

                chat_channel = team_chat_channel(guild, team)
                if chat_channel:
                    await chat_channel.send(f"**Announcement from {interaction.user.name}:**\n\n{message}")
                    sent_count += 1
                else:
                    # Queue announcement for later delivery when channel is created
                    await QueuedAnnouncement.objects.acreate(
                        team=team,
                        message=message,
                        sender_name=interaction.user.name,
                    )
                    queued_count += 1

            except Exception as e:
                logger.exception(f"Failed to broadcast to team {team_number}: {e}")
                failed_channels.append(f"Team {team_number:02d} ({str(e)[:50]})")
                continue

        await AuditLog.objects.acreate(
            action="broadcast_message",
            admin_user=str(interaction.user),
            target_entity="broadcast",
            target_id=0,
            details={
                "target": target,
                "message_preview": message[:200],
                "sent_count": sent_count,
                "queued_count": queued_count,
                "failed_count": len(failed_channels),
            },
        )

        ops_msg_parts = [
            f"Broadcast by {interaction.user.mention}",
            f"• Target: {target}",
            f"• Sent: {sent_count} channels",
        ]
        if queued_count > 0:
            ops_msg_parts.append(f"• Queued: {queued_count} (pending channel creation)")
        if failed_channels:
            ops_msg_parts.append(f"• Failed: {len(failed_channels)}")
        ops_msg_parts.append(f"Message: {message[:100]}...")

        await log_to_ops_channel(self.bot, "\n".join(ops_msg_parts))

        result_msg = f"Broadcast complete\n• Sent: {sent_count} channels"
        if queued_count > 0:
            result_msg += f"\n• Queued: {queued_count} (will deliver when team channels are created)"
        if failed_channels:
            result_msg += f"\n• Failed: {len(failed_channels)}"
            if len(failed_channels) <= 10:
                result_msg += "\n\nFailed:\n" + "\n".join([f"• {fc}" for fc in failed_channels])
            else:
                result_msg += "\n\nFailed (first 10):\n" + "\n".join([f"• {fc}" for fc in failed_channels[:10]])

        await interaction.followup.send(result_msg, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AdminCompetitionCog(bot))
