"""A ticket's Discord thread: finding it, and giving an open or claimed ticket one with its ticket message, team
and assignee."""

import asyncio
import logging

import discord
from asgiref.sync import sync_to_async
from django.conf import settings
from django.db.models import Q

from bot.ticket_dashboard import TicketActionView, format_ticket_embed, trigger_dashboard
from bot.utils import DISCORD_THREAD_NAME_CHAR_LIMIT, THREAD_AUTO_ARCHIVE_MINUTES, fit, team_chat_channel
from core.auth_utils import get_authentik_groups, team_for_groups
from core.discord_tasks import PermanentTaskError
from team.models import DiscordLink
from ticketing.models import Ticket

logger = logging.getLogger(__name__)

LIVE_STATUSES = (Ticket.STATUS_OPEN, Ticket.STATUS_CLAIMED)
# How long a task whose thread lost the race waits for the winner's ticket message
RACE_POLLS, RACE_POLL_SECONDS = 10, 0.5


class NotATicketThreadError(PermanentTaskError):
    """A ticket's stored thread ID names no competition thread the bot can see."""


async def ticket_thread(bot: discord.Client, ticket: Ticket) -> discord.Thread | None:
    """The ticket's thread, or None (a thread Discord says is gone has its ID cleared); raises NotATicketThreadError
    for an ID that isn't a competition thread the bot can see."""
    thread_id = ticket.discord_thread_id
    if not thread_id:
        return None
    thread = bot.get_channel(thread_id)
    if not thread:
        try:
            thread = await bot.fetch_channel(thread_id)
        except discord.NotFound:
            logger.warning(f"Thread {thread_id} of ticket {ticket.ticket_number} is gone; forgetting it")
            await Ticket.objects.filter(pk=ticket.pk, discord_thread_id=thread_id).aupdate(
                discord_thread_id=None, discord_ticket_message_id=None
            )
            ticket.discord_thread_id = ticket.discord_ticket_message_id = None
            return None
        except discord.Forbidden as e:
            raise NotATicketThreadError(
                f"Ticket {ticket.ticket_number}'s thread {thread_id} is hidden from the bot"
            ) from e
    if not isinstance(thread, discord.Thread) or thread.guild.id != settings.COMPETITION_GUILD_ID:
        raise NotATicketThreadError(f"Ticket {ticket.ticket_number}'s channel {thread_id} is not a competition thread")
    return thread


async def ensure_ticket_thread(bot: discord.Client, guild: discord.Guild, ticket: Ticket) -> discord.Thread | None:
    """The ticket's thread, created for an open or claimed ticket that has none; until its message ID is stored, the
    team and assignee are added and the message posted or adopted. Needs ``ticket.team``."""
    thread = await ticket_thread(bot, ticket)
    if ticket.status not in LIVE_STATUSES:
        return thread
    created = lost = False
    if thread is None:
        thread, created = await _create_thread(bot, guild, ticket)
        if thread is None:
            return None
        lost = not created
    if ticket.discord_ticket_message_id:
        return thread
    found = None if created else await _find_ticket_message(bot, thread, ticket)
    # Another task may be posting it; one slower than the wait still leads to two
    if not created and not found and await _recorded_message_id(ticket, polls=RACE_POLLS if lost else 1):
        return thread

    for member_id in await _member_ids(ticket):
        member = guild.get_member(member_id)
        if member:
            try:
                await thread.add_user(member)
            except discord.HTTPException as e:
                logger.warning(f"Failed to add member {member_id} to thread {thread.id}: {e}")
    if found:
        await _store_ticket_message(ticket, found)
        return thread

    message = await thread.send(
        f"**Ticket #{ticket.ticket_number}**: use the buttons below to manage this ticket.",
        embed=await sync_to_async(format_ticket_embed)(ticket),
        view=TicketActionView(ticket.id),
    )
    await _store_ticket_message(ticket, message.id)
    try:
        await message.pin()
    except discord.HTTPException as e:
        logger.warning(f"Failed to pin ticket message in thread {thread.id}: {e}")
    return thread


async def add_to_team_threads(bot: discord.Client, member: discord.Member, team_id: int) -> None:
    tickets = Ticket.objects.filter(team_id=team_id, status__in=LIVE_STATUSES, discord_thread_id__isnull=False)
    async for ticket in tickets:
        try:
            thread = await ticket_thread(bot, ticket)
            if thread:
                await thread.add_user(member)
        except (discord.HTTPException, NotATicketThreadError) as e:
            logger.warning(f"Could not add {member} to ticket {ticket.ticket_number}'s thread: {e}")


async def _find_ticket_message(bot: discord.Client, thread: discord.Thread, ticket: Ticket) -> int | None:
    """The bot's ticket message already in a thread made before its ID was stored (any wording): pinned, or among
    the thread's first messages."""
    start = f"**Ticket #{ticket.ticket_number}**"
    bot_id = bot.user.id if bot.user else None
    for messages in (thread.pins(), thread.history(limit=50, oldest_first=True)):
        async for message in messages:
            if message.author.id == bot_id and message.content.startswith(start):
                return message.id
    return None


async def _recorded_message_id(ticket: Ticket, polls: int) -> int | None:
    for poll in range(polls):
        if poll:
            await asyncio.sleep(RACE_POLL_SECONDS)
        await ticket.arefresh_from_db(fields=["discord_ticket_message_id"])
        if ticket.discord_ticket_message_id:
            break
    return ticket.discord_ticket_message_id


async def _store_ticket_message(ticket: Ticket, message_id: int) -> None:
    ticket.discord_ticket_message_id = message_id
    await Ticket.objects.filter(pk=ticket.pk).aupdate(discord_ticket_message_id=message_id)


async def _create_thread(
    bot: discord.Client, guild: discord.Guild, ticket: Ticket
) -> tuple[discord.Thread | None, bool]:
    """A new thread for the ticket, and whether it is the one stored: when another task stored one meanwhile,
    that thread is the ticket's and the new one is deleted."""
    team = ticket.team
    chat_channel = team_chat_channel(guild, team)
    if not chat_channel:
        logger.warning(f"No chat channel found for {team.team_name} (category {team.discord_category_id})")
        return None, False
    thread = await chat_channel.create_thread(
        name=fit(
            f"{ticket.ticket_number}: Team {team.team_number:02d}, {ticket.title}", DISCORD_THREAD_NAME_CHAR_LIMIT
        ),
        auto_archive_duration=THREAD_AUTO_ARCHIVE_MINUTES,
        invitable=False,
    )
    stored = await Ticket.objects.filter(pk=ticket.pk, discord_thread_id__isnull=True).aupdate(
        discord_thread_id=thread.id, discord_ticket_message_id=None
    )
    if stored:
        ticket.discord_thread_id, ticket.discord_ticket_message_id = thread.id, None
        # A claim made while the thread was being created
        await ticket.arefresh_from_db(fields=["assigned_to"])
        logger.info(f"Created thread {thread.id} for ticket #{ticket.ticket_number}")
        return thread, True
    await thread.delete()
    await ticket.arefresh_from_db(fields=["discord_thread_id", "discord_ticket_message_id"])
    return await ticket_thread(bot, ticket), False


@sync_to_async
def _member_ids(ticket: Ticket) -> list[int]:
    """The Discord IDs of the ticket's team members and its assignee, unless the assignee is a team account (it
    would pull another team into the thread)."""
    people = Q(team_id=ticket.team_id)
    if ticket.assigned_to and not team_for_groups(get_authentik_groups(ticket.assigned_to)):
        people |= Q(user_id=ticket.assigned_to_id)
    return list(DiscordLink.objects.filter(people, is_active=True).values_list("discord_id", flat=True))


async def publish_new_ticket(bot: discord.Client, guild: discord.Guild | None, ticket: Ticket) -> None:
    """Give a ticket created in Discord its team thread and put it on the dashboard.

    ``ticket.team`` must be loaded. Thread failures are logged, not raised: the
    ticket already exists and still belongs on the dashboard.
    """
    if guild is None:
        logger.warning(f"No guild available; ticket {ticket.ticket_number} will have no thread")
    else:
        try:
            await ensure_ticket_thread(bot, guild, ticket)
        except Exception:
            logger.exception(f"Failed to create thread for ticket {ticket.ticket_number}")
    trigger_dashboard(bot)
