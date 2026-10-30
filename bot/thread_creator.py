"""Shared utility for creating Discord ticket threads."""

import logging

import discord
from asgiref.sync import sync_to_async

from bot.utils import DISCORD_THREAD_NAME_CHAR_LIMIT, THREAD_AUTO_ARCHIVE_MINUTES, fit, team_chat_channel
from team.models import Team
from ticketing.models import Ticket

logger = logging.getLogger(__name__)


async def create_ticket_thread(
    guild: discord.Guild,
    ticket: Ticket,
) -> discord.Thread | None:
    """Create a Discord thread for a ticket in the team's chat channel.

    Saves the thread ID on the ticket, adds active team members, and posts the ticket message.

    Returns the thread, or None if the category or chat channel could not be
    found. Discord API errors propagate.
    """

    team = ticket.team
    chat_channel = team_chat_channel(guild, team)
    if not chat_channel:
        logger.warning(f"No chat channel found for {team.team_name} (category {team.discord_category_id})")
        return None

    thread = await chat_channel.create_thread(
        name=fit(
            f"{ticket.ticket_number}: Team {team.team_number:02d}, {ticket.title}", DISCORD_THREAD_NAME_CHAR_LIMIT
        ),
        auto_archive_duration=THREAD_AUTO_ARCHIVE_MINUTES,
    )

    # Targeted update: a full save of this instance would revert concurrent web changes.
    ticket.discord_thread_id = thread.id
    await Ticket.objects.filter(pk=ticket.pk).aupdate(discord_thread_id=thread.id)

    await add_team_members(guild, thread, team)
    await post_ticket_message(thread, ticket)
    logger.info(f"Created thread {thread.id} for ticket #{ticket.ticket_number}")
    return thread


async def add_team_members(guild: discord.Guild, thread: discord.Thread, team: Team) -> None:
    """Add the team's linked members to its private ticket thread; adding one already in it changes nothing."""
    from bot.utils import get_team_member_discord_ids

    for member_id in await get_team_member_discord_ids(team):
        try:
            member = guild.get_member(member_id)
            if member:
                await thread.add_user(member)
        except Exception as e:
            logger.warning(f"Failed to add member {member_id} to thread {thread.id}: {e}")


async def post_ticket_message(thread: discord.Thread | discord.TextChannel, ticket: Ticket) -> None:
    from bot.ticket_dashboard import TicketActionView, format_ticket_embed

    embed = await sync_to_async(format_ticket_embed)(ticket)
    message = await thread.send(
        f"**Ticket #{ticket.ticket_number}**: use the buttons below to manage this ticket.",
        embed=embed,
        view=TicketActionView(ticket.id),
    )
    try:
        await message.pin()
    except Exception as pin_error:
        logger.warning(f"Failed to pin ticket message in thread {thread.id}: {pin_error}")
