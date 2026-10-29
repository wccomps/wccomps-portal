"""Ticketing commands for team support."""

import logging

import discord
from asgiref.sync import sync_to_async
from discord import app_commands
from discord.ext import commands, tasks
from django.utils import timezone
from scoring.quotient_sync import ticket_infrastructure

from bot.permissions import check_blue_team, linked_team_member
from bot.ticket_input import file_ticket
from bot.utils import add_field, choices, pinging, run_blocking
from core.stored_files import clean_name
from core.utils import shorten
from ticketing.models import CommentRateLimit, Ticket, TicketAttachment, TicketCategory, TicketComment, TicketHistory
from ticketing.utils import get_user_for_ticket

logger = logging.getLogger(__name__)


def storable(text: str) -> str:
    """A message's text as a comment stores it: without NUL characters, which PostgreSQL text can't hold. The
    message is already in Discord, so it is kept rather than refused."""
    return text.replace("\x00", "")


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
                if not await Ticket.objects.filter(pk=ticket.pk, status__in=["resolved", "cancelled"]).aexists():
                    continue
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

    async def hostname_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        # The metadata the sync stores: one quick read a keystroke, no Quotient call
        box_names, box_ip_map, _ = await sync_to_async(ticket_infrastructure)()
        matches = [name for name in box_names if current.lower() in name.lower()]
        return choices([(f"{name} ({box_ip_map.get(name, '')})", name) for name in matches])

    async def service_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        _, _, service_choices = await sync_to_async(ticket_infrastructure)()
        matches = [s for s in service_choices if current.lower() in s["label"].lower()]
        return choices([(s["label"], s["value"]) for s in matches])

    async def category_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        categories = [
            cat async for cat in TicketCategory.objects.filter(user_creatable=True).order_by("sort_order", "id")
        ]
        named = [(f"{cat.display_name} ({cat.points_label})", str(cat.id)) for cat in categories]
        return choices([(name, value) for name, value in named if not current or current.lower() in name.lower()])

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
        # Discord rejects a first answer after 3 seconds
        await interaction.response.defer(ephemeral=True)

        member = await linked_team_member(interaction.user.id)
        if not member:
            await interaction.followup.send("Your Discord account is no longer linked to a team.", ephemeral=True)
            return
        team = member.team

        filed = await run_blocking(
            file_ticket,
            team,
            category,
            staff=False,
            actor=f"discord:{interaction.user}",
            description=description,
            hostname=hostname or "",
            ip_address=ip_address or "",
            service_name=service or "",
        )
        if isinstance(filed, str):
            await interaction.followup.send(filed, ephemeral=True)
            return
        ticket, cat = filed
        logger.info(f"Ticket {ticket.ticket_number} created by {interaction.user} for {team.team_name}")

        embed = discord.Embed(
            title="Ticket Created",
            description=f"Your {cat.display_name} ticket has been created.",
            color=discord.Color.green(),
        )
        embed.add_field(name="Ticket Number", value=ticket.ticket_number, inline=True)
        embed.add_field(name="Status", value="Open", inline=True)
        add_field(embed, "Team", team.team_name, inline=True)
        embed.add_field(name="Point Cost", value=cat.points_display, inline=True)
        add_field(embed, "Description", ticket.description)

        embed.add_field(
            name="Need to attach files?",
            value="Post screenshots, logs, or other files directly in the ticket thread.\n"
            "Find the thread in your team channels or #ticket-queue.",
            inline=False,
        )

        embed.set_footer(text="Volunteers will be notified in #ticket-queue")

        await interaction.followup.send(embed=embed, ephemeral=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Sync ticket thread messages and attachments to the ticket, with rate limiting."""
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

        if content := storable(message.content):
            existing = await TicketComment.objects.filter(discord_message_id=message.id).afirst()

            if not existing:
                author = await sync_to_async(get_user_for_ticket)(discord_id=message.author.id)
                await TicketComment.objects.acreate(
                    ticket=ticket,
                    author=author,
                    comment_text=content,
                    discord_message_id=message.id,
                )
                logger.info(f"Saved Discord message as comment for ticket #{ticket.id}")

        if message.attachments:
            for attachment in message.attachments:
                # Stored as a web upload is: the name cleaned, the type from the bytes (or the name), never the type
                # the client claimed (core.stored_files)
                filename = clean_name(attachment.filename, "attachment")
                if attachment.size > TicketAttachment.MAX_SIZE:
                    await message.channel.send(
                        f"{message.author.mention} File `{shorten(filename, 100)}` is too large (max 10MB). "
                        "Please use a file sharing service.",
                        delete_after=30,
                        allowed_mentions=pinging(users=[message.author]),
                    )
                    continue

                try:
                    await TicketAttachment.astore(ticket, await attachment.read(), filename, str(message.author))

                    logger.info(f"Stored attachment {filename} for ticket #{ticket.id}")

                    await message.add_reaction("📎")

                except Exception as e:
                    logger.exception(f"Failed to store attachment {filename}: {e}")
                    await message.channel.send(
                        f"{message.author.mention} Failed to store attachment `{shorten(filename, 100)}`. "
                        "Please try again.",
                        delete_after=30,
                        allowed_mentions=pinging(users=[message.author]),
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
            comment.comment_text = storable(after.content)
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
