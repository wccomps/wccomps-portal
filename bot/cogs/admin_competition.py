import io
import logging

import discord
from asgiref.sync import sync_to_async
from discord import app_commands
from discord.ext import commands
from django.conf import settings

from bot.competition_actions import deliver_team_broadcast, run_competition_cleanup
from bot.permissions import permission_check
from bot.utils import ConfirmView, log_to_ops_channel, run_blocking, split_message
from core.authentik_utils import (
    credentials_csv,
    parse_team_range,
    reset_teams_credentials,
    sessions_failure_summary,
)
from core.forms import AppSlugForm
from core.models import MAX_TEAM_MEMBERS_LIMIT, AuditLog, CompetitionConfig
from core.services.competition import KEEP, ConfigEditError, set_controlled_apps, set_max_members, set_schedule
from core.utils import (
    DEFAULT_TIMEZONE,
    DEFAULT_TIMEZONE_NAME,
    TIMEZONE_CHOICES,
    UnknownTimezoneError,
    parse_datetime_to_utc,
    shorten,
)
from team.models import active_team_numbers

logger = logging.getLogger(__name__)

TIMEZONE_OPTIONS = [app_commands.Choice(name=label, value=zone) for zone, label in TIMEZONE_CHOICES]


class AdminCompetitionCog(commands.Cog):
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
                f"**WARNING: You are about to reset passwords for ALL {len(active)} active team accounts.**\n\n"
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
        result = await sync_to_async(reset_teams_credentials)(teams, actor=str(interaction.user))
        password_list = result.password_list
        failed_resets = result.failed_resets
        sessions_failed = result.sessions_failed
        total = len(teams)

        summary = f"Password reset complete\n• Success: {len(password_list)}/{total}\n"
        details = "".join(f"• Failed {username}: {error}\n" for username, error in failed_resets)
        if sessions_failed:
            details += f"• Sessions: {sessions_failure_summary(sessions_failed)}\n"
        if result.sessions_uncounted:
            details += f"• Could not count the sessions of {', '.join(result.sessions_uncounted)}\n"
        if password_list:
            # The CSV rides on the short first message, so details past Discord's limit can't lose the passwords
            csv_content = credentials_csv((username, password) for _num, username, password in password_list)
            file = discord.File(fp=io.BytesIO(csv_content.encode("utf-8")), filename="blueteam_passwords.csv")
            await interaction.followup.send(
                summary + "\nCSV attached with the new credentials.", file=file, ephemeral=True
            )
        else:
            details = summary + details
        for part in split_message(details) if details else []:
            await interaction.followup.send(part, ephemeral=True)

        logger.info(f"Password reset performed by {interaction.user}. Failed: {len(failed_resets)}")

    @competition_group.command(name="set-max-members", description="[ADMIN] Set maximum team members globally")
    @app_commands.describe(max_members=f"Maximum members per team (1-{MAX_TEAM_MEMBERS_LIMIT})")
    @app_commands.check(permission_check("admin"))
    async def admin_set_max_members(self, interaction: discord.Interaction, max_members: int) -> None:
        """Set global maximum team members."""
        await interaction.response.defer(ephemeral=True)
        try:
            old_max = await run_blocking(set_max_members, max_members, str(interaction.user))
        except ConfigEditError as e:
            await interaction.followup.send(f"{e}.", ephemeral=True)
            return

        await log_to_ops_channel(
            self.bot,
            f"Max Team Members Updated by {interaction.user.mention}\n• Old: {old_max}\n• New: {max_members}",
        )

        await interaction.followup.send(
            f"Maximum team members set to {max_members} (was {old_max}).",
            ephemeral=True,
        )

    async def _set_competition_time(
        self,
        interaction: discord.Interaction,
        datetime_str: str,
        timezone_name: str,
        *,
        is_start: bool,
    ) -> None:
        label = "Start" if is_start else "End"
        label_lower = label.lower()
        action_verb = "enabled" if is_start else "disabled"
        example_hour = "09:00" if is_start else "17:00"
        try:
            scheduled_time = parse_datetime_to_utc(datetime_str, timezone_name)
        except UnknownTimezoneError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        except ValueError:
            await interaction.response.send_message(
                f"Invalid datetime format. Use: YYYY-MM-DDTHH:MM (e.g., 2025-01-15T{example_hour})",
                ephemeral=True,
            )
            return

        start, end = (scheduled_time, KEEP) if is_start else (KEEP, scheduled_time)
        await interaction.response.defer(ephemeral=True)
        try:
            config = await run_blocking(set_schedule, str(interaction.user), start=start, end=end)
        except ConfigEditError as e:
            await interaction.followup.send(str(e), ephemeral=True)
            return

        await log_to_ops_channel(
            self.bot,
            f"Competition {label} Time Set by {interaction.user.mention}\n"
            f"• {label} Time: {discord.utils.format_dt(scheduled_time, style='F')}\n"
            f"• Controlled Apps: {', '.join(config.controlled_applications)}\n"
            f"• Applications will be {action_verb} automatically at {label_lower} time",
        )

        await interaction.followup.send(
            f"Competition {label_lower} time set to: {discord.utils.format_dt(scheduled_time, style='F')}\n"
            f"Controlled applications: {', '.join(config.controlled_applications)}\n\n"
            f"Applications will be automatically {action_verb} at {label_lower} time.",
            ephemeral=True,
        )

    @competition_group.command(
        name="set-start-time",
        description="[ADMIN] Set competition start time (applications will be enabled automatically)",
    )
    @app_commands.describe(
        datetime_str="Start time in format: YYYY-MM-DDTHH:MM (e.g., 2025-01-15T09:00)",
        timezone_name=f"Timezone (defaults to {DEFAULT_TIMEZONE_NAME})",
    )
    @app_commands.choices(timezone_name=TIMEZONE_OPTIONS)
    @app_commands.check(permission_check("admin"))
    async def admin_set_start_time(
        self,
        interaction: discord.Interaction,
        datetime_str: str,
        timezone_name: str = DEFAULT_TIMEZONE,
    ) -> None:
        """Set competition start time for automatic application enabling."""
        await self._set_competition_time(interaction, datetime_str, timezone_name, is_start=True)

    @competition_group.command(
        name="set-end-time",
        description="[ADMIN] Set competition end time (applications will be disabled automatically)",
    )
    @app_commands.describe(
        datetime_str="End time in format: YYYY-MM-DDTHH:MM (e.g., 2025-01-15T17:00)",
        timezone_name=f"Timezone (defaults to {DEFAULT_TIMEZONE_NAME})",
    )
    @app_commands.choices(timezone_name=TIMEZONE_OPTIONS)
    @app_commands.check(permission_check("admin"))
    async def admin_set_end_time(
        self,
        interaction: discord.Interaction,
        datetime_str: str,
        timezone_name: str = DEFAULT_TIMEZONE,
    ) -> None:
        """Set competition end time for automatic application disabling."""
        await self._set_competition_time(interaction, datetime_str, timezone_name, is_start=False)

    @competition_group.command(
        name="clear-start-time",
        description="[ADMIN] Cancel the scheduled start (the competition won't start on its own)",
    )
    @app_commands.check(permission_check("admin"))
    async def admin_clear_start_time(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await run_blocking(set_schedule, str(interaction.user), start=None)
        await log_to_ops_channel(self.bot, f"Scheduled start cleared by {interaction.user.mention}")
        await interaction.followup.send(
            "Scheduled start cleared: the competition won't start on its own.", ephemeral=True
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
            "• Delete all team Discord channels and categories\n"
            "• Remove team roles from all members\n"
            "• Deactivate all team member Discord links\n"
            "• Remove the Room Judge role\n"
            "• Clear queued announcements\n\n"
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
    async def admin_competition_set_apps(
        self, interaction: discord.Interaction, app_slugs: app_commands.Range[str, 1, 1000]
    ) -> None:
        """Set which applications to control."""

        slugs = []
        for raw in app_slugs.split(","):
            if not raw.strip():
                continue
            form = AppSlugForm({"app_slug": raw})
            if not form.is_valid():
                await interaction.response.send_message(
                    f"Not an application slug: {shorten(raw.strip(), 40)} "
                    "(use 1 to 100 letters, digits, hyphens and underscores).",
                    ephemeral=True,
                )
                return
            slugs.append(form.cleaned_data["app_slug"])

        if not slugs:
            await interaction.response.send_message("Please provide at least one application slug.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        await run_blocking(set_controlled_apps, lambda _: slugs, str(interaction.user))

        await log_to_ops_channel(
            self.bot,
            f"Competition Applications Configured by {interaction.user.mention}\n• Applications: {', '.join(slugs)}",
        )

        await interaction.followup.send(f"Controlled applications set to: {', '.join(slugs)}", ephemeral=True)

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

        try:
            sent_count, queued_count, failed_channels = await deliver_team_broadcast(
                guild, target, message, interaction.user.name
            )
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

        await AuditLog.objects.acreate(
            action="broadcast_message",
            admin_user=str(interaction.user),
            target_entity="broadcast",
            target_id=0,
            details={
                "target": target,
                "message_preview": shorten(message, 200),
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
        ops_msg_parts.append(f"Message: {shorten(message, 100)}")

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
