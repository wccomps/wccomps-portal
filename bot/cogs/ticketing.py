"""Ticketing commands for team support."""

import logging

import discord
from asgiref.sync import sync_to_async
from discord import app_commands
from discord.ext import commands, tasks
from django.utils import timezone
from quotient.client import get_quotient_client

from bot.permissions import check_blue_team, linked_team_member
from bot.thread_creator import publish_new_ticket
from bot.utils import DISCORD_EMBED_FIELD_CHAR_LIMIT, fit, pinging
from ticketing.models import CommentRateLimit, Ticket, TicketAttachment, TicketCategory, TicketComment, TicketHistory
from ticketing.utils import TicketRateLimitError, acreate_ticket_atomic, get_user_for_ticket

logger = logging.getLogger(__name__)


class TicketingCog(commands.Cog):
    """Commands for creating and managing tickets."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.archive_threads_task.start()

    async def cog_unload(self) -> None:
        self.archive_threads_task.cancel()

    @tasks.loop(minutes=1)
    async def archive_threads_task(self) -> None:
        """Archive the threads of resolved and cancelled tickets once their scheduled time passes."""
        try:
            now = timezone.now()
            tickets_to_archive = [
                ticket
                async for ticket in Ticket.objects.filter(
                    thread_archive_scheduled_at__lte=now,
                    discord_thread_id__isnull=False,
                    status__in=["resolved", "cancelled"],
                )
            ]

            for ticket in tickets_to_archive:
                try:
                    thread = self.bot.get_channel(ticket.discord_thread_id) if ticket.discord_thread_id else None
                    if isinstance(thread, discord.Thread):
                        await thread.edit(archived=True, locked=True)
                        await TicketHistory.objects.acreate(
                            ticket=ticket,
                            action="thread_archived",
                            details={"archived_at": str(now), "actor": "System"},
                        )
                        logger.info(f"Archived thread for ticket #{ticket.id}")
                except Exception as e:
                    logger.exception(f"Failed to archive thread for ticket #{ticket.id}: {e}")
                # Cleared whatever happened, so a failure isn't retried every minute. Only this field:
                # a full save of the stale row would undo a reopen made while the thread was edited.
                await Ticket.objects.filter(pk=ticket.pk).aupdate(thread_archive_scheduled_at=None)

        except Exception as e:
            logger.exception(f"Error in archive_threads_task: {e}")

    @archive_threads_task.before_loop
    async def before_archive_threads_task(self) -> None:
        await self.bot.wait_until_ready()

    async def _get_infrastructure_data(self) -> tuple[list[str], dict[str, str], list[dict[str, str]]]:
        """Get infrastructure data from Quotient, off the event loop (a cache miss is an HTTP call)."""
        return await sync_to_async(self._load_infrastructure_data)()

    @staticmethod
    def _load_infrastructure_data() -> tuple[list[str], dict[str, str], list[dict[str, str]]]:
        try:
            client = get_quotient_client()
            infrastructure = client.get_infrastructure()
            if not infrastructure:
                return [], {}, []

            box_names = [box.name for box in infrastructure.boxes]
            box_ip_map = {box.name: box.ip for box in infrastructure.boxes}
            service_choices = client.get_service_choices()
            return box_names, box_ip_map, service_choices
        except Exception as e:
            logger.warning(f"Failed to get infrastructure data: {e}")
            return [], {}, []

    async def hostname_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        box_names, box_ip_map, _ = await self._get_infrastructure_data()
        matches = [name for name in box_names if current.lower() in name.lower()]
        return [app_commands.Choice(name=f"{name} ({box_ip_map.get(name, '')})", value=name) for name in matches[:25]]

    async def service_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        _, _, service_choices = await self._get_infrastructure_data()
        matches = [s for s in service_choices if current.lower() in s["label"].lower()]
        return [app_commands.Choice(name=s["label"], value=s["value"]) for s in matches[:25]]

    async def category_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        from core.tickets_config import get_all_categories

        categories = await sync_to_async(get_all_categories)(user_creatable_only=True)
        choices = []
        for cat_id, cat_info in categories.items():
            name = f"{cat_info['display_name']} ({cat_info.get('points', 0)}pt)"
            if not current or current.lower() in name.lower():
                choices.append(app_commands.Choice(name=name, value=str(cat_id)))
        return choices[:25]

    @app_commands.command(name="ticket", description="[BLUE TEAM] Create a support ticket for your team")
    @app_commands.describe(
        category="Type of support needed",
        description="Describe the issue or request",
        hostname="Hostname/box name (required for box-reset, hands-on consultation)",
        service="Service name like 'web:http' (required for scoring validation, service check)",
        ip_address="IP address (auto-filled from hostname, or enter manually)",
    )
    @app_commands.autocomplete(
        category=category_autocomplete, hostname=hostname_autocomplete, service=service_autocomplete
    )
    @app_commands.check(check_blue_team)
    async def create_ticket(
        self,
        interaction: discord.Interaction,
        category: str,
        description: str,
        hostname: str | None = None,
        service: str | None = None,
        ip_address: str | None = None,
    ) -> None:
        """Create a support ticket."""
        if not interaction.guild:
            await interaction.response.send_message("This command must be used in a guild", ephemeral=True)
            return

        member = await linked_team_member(interaction.user.id)
        if not member:
            return
        team = member.team

        from core.tickets_config import get_category_config

        category_id = int(category)
        cat_info = await sync_to_async(get_category_config)(category_id)
        if not cat_info:
            await interaction.response.send_message("Invalid ticket category.", ephemeral=True)
            return

        required_fields = cat_info.get("required_fields", [])
        missing_fields = []

        if "hostname" in required_fields and not hostname:
            missing_fields.append("hostname")
        if "service_name" in required_fields and not service:
            missing_fields.append("service (e.g., 'web:http')")
        # IP address can be auto-populated from hostname, so only require if no hostname provided
        if "ip_address" in required_fields and not ip_address and not hostname:
            missing_fields.append("ip_address (or select a hostname)")
        if "description" in required_fields and not description.strip():
            missing_fields.append("description")

        if missing_fields:
            await interaction.response.send_message(
                f"**{cat_info['display_name']}** requires: {', '.join(missing_fields)}\n"
                f"Please provide these fields when creating the ticket.",
                ephemeral=True,
            )
            return

        resolved_ip = ip_address
        if hostname and not ip_address:
            _, box_ip_map, _ = await self._get_infrastructure_data()
            resolved_ip = box_ip_map.get(hostname)

        if service and not hostname:
            _, _, service_choices = await self._get_infrastructure_data()
            for svc in service_choices:
                if svc["value"] == service:
                    hostname = svc.get("box_name", "")
                    if not resolved_ip:
                        resolved_ip = svc.get("box_ip")
                    break

        # Create ticket atomically to prevent race conditions
        category_obj = await TicketCategory.objects.aget(pk=category_id)
        try:
            ticket = await acreate_ticket_atomic(
                team=team,
                category=category_obj,
                title=cat_info["display_name"],
                description=description,
                hostname=hostname or "",
                ip_address=resolved_ip,
                service_name=service or "",
                actor_username=f"discord:{interaction.user}",
            )
        except TicketRateLimitError as e:
            logger.warning(f"Ticket rate limit hit by {interaction.user} for {team.team_name}")
            await interaction.response.send_message(str(e), ephemeral=True)
            return

        embed = discord.Embed(
            title="Ticket Created",
            description=f"Your {cat_info['display_name']} ticket has been created.",
            color=discord.Color.green(),
        )
        embed.add_field(name="Ticket Number", value=ticket.ticket_number, inline=True)
        embed.add_field(name="Status", value="Open", inline=True)
        embed.add_field(name="Team", value=team.team_name, inline=True)
        embed.add_field(name="Point Cost", value=f"{cat_info.get('points', 0)} points", inline=True)
        embed.add_field(name="Description", value=fit(description, DISCORD_EMBED_FIELD_CHAR_LIMIT), inline=False)

        embed.add_field(
            name="Need to attach files?",
            value="Post screenshots, logs, or other files directly in the ticket thread.\n"
            "Find the thread in your team channels or #ticket-queue.",
            inline=False,
        )

        embed.set_footer(text="Volunteers will be notified in #ticket-queue")

        await interaction.response.send_message(embed=embed, ephemeral=True)
        logger.info(f"Ticket {ticket.ticket_number} created by {interaction.user} for {team.team_name}")

        await publish_new_ticket(self.bot, interaction.guild, ticket)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Handle messages in ticket threads (attachments and rate limiting)."""
        if message.author.bot:
            return

        if not isinstance(message.channel, discord.Thread):
            return

        ticket = await Ticket.objects.filter(discord_thread_id=message.channel.id).afirst()
        if not ticket:
            return

        is_allowed, reason = await sync_to_async(CommentRateLimit.check_rate_limit)(ticket.id, message.author.id)

        if not is_allowed:
            try:
                await message.delete()
                await message.channel.send(
                    f"{message.author.mention} {reason}. Please slow down.",
                    delete_after=10,
                    allowed_mentions=pinging(users=[message.author]),
                )
                logger.warning(f"Rate limit exceeded for user {message.author.id} on ticket #{ticket.id}")
                return
            except discord.Forbidden:
                logger.warning("Cannot delete message due to permissions")
                return

        await CommentRateLimit.objects.acreate(ticket=ticket, discord_id=message.author.id)

        # Save message as comment in database (for web interface visibility)
        if message.content:
            existing = await TicketComment.objects.filter(discord_message_id=message.id).afirst()

            if not existing:
                author = await sync_to_async(get_user_for_ticket)(discord_id=message.author.id)
                await TicketComment.objects.acreate(
                    ticket=ticket,
                    author=author,
                    comment_text=message.content,
                    discord_message_id=message.id,
                )
                logger.info(f"Saved Discord message as comment for ticket #{ticket.id}")

        if message.attachments:
            for attachment in message.attachments:
                if attachment.size > 10 * 1024 * 1024:
                    await message.channel.send(
                        f"{message.author.mention} File `{attachment.filename}` is too large (max 10MB). "
                        "Please use a file sharing service.",
                        delete_after=30,
                    )
                    continue

                try:
                    file_data = await attachment.read()

                    await TicketAttachment.objects.acreate(
                        ticket=ticket,
                        file_data=file_data,
                        filename=attachment.filename,
                        mime_type=attachment.content_type or "application/octet-stream",
                        uploaded_by=str(message.author),
                    )

                    logger.info(f"Stored attachment {attachment.filename} for ticket #{ticket.id}")

                    await message.add_reaction("📎")

                except Exception as e:
                    logger.exception(f"Failed to store attachment {attachment.filename}: {e}")
                    await message.channel.send(
                        f"{message.author.mention} Failed to store attachment `{attachment.filename}`. "
                        "Please try again.",
                        delete_after=30,
                    )

    @commands.Cog.listener()
    async def on_message_edit(self, _before: discord.Message, after: discord.Message) -> None:
        """Sync message edits to TicketComment."""
        if after.author.bot:
            return

        if not isinstance(after.channel, discord.Thread):
            return

        ticket = await Ticket.objects.filter(discord_thread_id=after.channel.id).afirst()
        if not ticket:
            return

        comment = await TicketComment.objects.filter(ticket=ticket, discord_message_id=after.id).afirst()

        if comment:
            comment.comment_text = after.content
            await comment.asave()

            logger.info(f"Synced edit to comment {comment.id} for ticket #{ticket.id}")

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message) -> None:
        """Mark TicketComment as deleted when Discord message is deleted."""
        if message.author.bot:
            return

        if not isinstance(message.channel, discord.Thread):
            return

        ticket = await Ticket.objects.filter(discord_thread_id=message.channel.id).afirst()
        if not ticket:
            return

        comment = await TicketComment.objects.filter(ticket=ticket, discord_message_id=message.id).afirst()

        if comment:
            comment.comment_text = "[Message deleted]"
            await comment.asave()

            logger.info(f"Marked comment {comment.id} as deleted for ticket #{ticket.id}")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TicketingCog(bot))
