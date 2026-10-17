"""Scoring commands for CCDC competitions."""

import logging

import discord
from asgiref.sync import sync_to_async
from discord import app_commands
from discord.ext import commands
from django.core.exceptions import ValidationError
from django.utils import timezone
from scoring.models import IncidentReport

from bot.permissions import check_blue_team, linked_team_member
from bot.utils import add_field

logger = logging.getLogger(__name__)


class ScoringCog(commands.Cog):
    """Commands for scoring and incident reports."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="incident-report", description="[BLUE TEAM] Submit an incident report for detected red team activity"
    )
    @app_commands.describe(
        affected_box="Which box was affected (e.g., web-server, mail-server)",
        affected_service="Which service was attacked (e.g., HTTP, SSH, SMTP)",
        source_ip="Source IP address of the attack",
        attack_vector="Type of attack (e.g., SQL Injection, Brute Force)",
        description="Detailed description of what you detected and how you mitigated it",
    )
    @app_commands.check(check_blue_team)
    async def incident_report(
        self,
        interaction: discord.Interaction,
        affected_box: app_commands.Range[str, 1, 100],
        affected_service: app_commands.Range[str, 1, IncidentReport._meta.get_field("affected_service").max_length],
        source_ip: app_commands.Range[str, 1, 45],
        attack_vector: app_commands.Range[str, 1, 200],
        description: str,
    ) -> None:
        """Submit an incident report for red team activity."""
        member = await linked_team_member(interaction.user.id)
        if not member:
            return
        team, submitted_by_user = member.team, member.user

        if not source_ip.strip():
            await interaction.response.send_message(
                "Source IP address is required. Please include the IP address from your evidence.",
                ephemeral=True,
            )
            return

        if len(description) < 50:
            await interaction.response.send_message(
                "Description too short. Please provide a detailed explanation (at least 50 characters) including:\n"
                "• How you detected the attack\n"
                "• Evidence you collected (timestamp, IP, logs)\n"
                "• Mitigation steps you took",
                ephemeral=True,
            )
            return

        incident = IncidentReport(
            team=team,
            affected_boxes=[affected_box] if affected_box else [],
            affected_service=affected_service,
            source_ip=source_ip.strip(),
            destination_ip=None,
            attack_description=f"{attack_vector}\n\n{description}",
            attack_detected_at=timezone.now(),
            submitted_by=submitted_by_user,
        )
        try:
            # The model's own checks, which the web form runs too: an address the column takes, among them
            await sync_to_async(incident.full_clean)()
        except ValidationError as e:
            problems = [
                f"{name.replace('_', ' ')}: {error}" for name, errors in e.message_dict.items() for error in errors
            ]
            await interaction.response.send_message(" ".join(problems), ephemeral=True)
            return

        try:
            await incident.asave()

            embed = discord.Embed(
                title="Incident Report Submitted",
                description="Your incident report has been submitted for review.",
                color=discord.Color.blue(),
            )
            embed.add_field(name="Report ID", value=f"#{incident.id}", inline=True)
            embed.add_field(name="Team", value=team.team_name, inline=True)
            embed.add_field(name="Status", value="Pending Review", inline=True)
            add_field(embed, "Affected Box", affected_box, inline=True)
            add_field(embed, "Affected Service", affected_service, inline=True)
            add_field(embed, "Source IP", f"`{incident.source_ip}`", inline=True)
            add_field(embed, "Attack Vector", attack_vector)
            add_field(embed, "Description", description)

            embed.add_field(
                name="Evidence",
                value="Screenshots can only be attached on the portal's incident form, when a report is submitted. "
                "To include them, submit the report there instead.",
                inline=False,
            )

            embed.set_footer(
                text="Gold team will review and match this to red team findings. Points may be returned if validated."
            )

            await interaction.response.send_message(embed=embed, ephemeral=True)
            logger.info(f"Incident report #{incident.id} created by {interaction.user} for team {team.team_name}")

        except Exception as e:
            logger.exception(f"Failed to create incident report: {e}")
            await interaction.response.send_message(
                "Failed to submit incident report. Please try again or contact staff if the problem persists.",
                ephemeral=True,
            )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ScoringCog(bot))
