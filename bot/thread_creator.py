"""Shared utility for creating Discord ticket threads."""

import logging

import discord
from asgiref.sync import sync_to_async

from bot.utils import THREAD_AUTO_ARCHIVE_MINUTES, team_chat_channel
from core.utils import shorten
from team.models import Team
from ticketing.models import Ticket

logger = logging.getLogger(__name__)


THREAD_NAME_LIMIT = 100


def thread_name(ticket: Ticket, team: Team) -> str:
    """The ticket's thread name, cut to the length Discord takes."""
    return shorten(f"{ticket.ticket_number}: Team {team.team_number:02d}, {ticket.title}", THREAD_NAME_LIMIT)


async def create_ticket_thread(
    guild: discord.Guild,
    ticket: Ticket,
    team: Team,
    pin_message: bool = False,
) -> discord.Thread | None:
    """Create a Discord thread for a ticket in the team's chat channel.

    Saves the thread ID on the ticket, adds active team members, and posts the ticket embed with action buttons.

    Returns the thread, or None if the category or chat channel could not be
    found. Discord API errors propagate.
    """
    from bot.ticket_dashboard import TicketActionView, format_ticket_embed
    from bot.utils import get_team_member_discord_ids

    chat_channel = team_chat_channel(guild, team)
    if not chat_channel:
        logger.warning(f"No chat channel found for {team.team_name} (category {team.discord_category_id})")
        return None

    thread = await chat_channel.create_thread(
        name=thread_name(ticket, team),
        auto_archive_duration=THREAD_AUTO_ARCHIVE_MINUTES,
    )

    # Targeted update: a full save of this instance would revert concurrent web changes.
    ticket.discord_thread_id = thread.id
    ticket.discord_channel_id = team.discord_category_id
    await Ticket.objects.filter(pk=ticket.pk).aupdate(
        discord_thread_id=thread.id, discord_channel_id=team.discord_category_id
    )

    team_member_ids = await get_team_member_discord_ids(team)
    for member_id in team_member_ids:
        try:
            member = guild.get_member(member_id)
            if member:
                await thread.add_user(member)
        except Exception as e:
            logger.warning(f"Failed to add member {member_id} to thread {thread.id}: {e}")

    embed = await sync_to_async(format_ticket_embed)(ticket)
    view = TicketActionView(ticket.id)
    message = await thread.send(
        f"**Ticket #{ticket.ticket_number}**: use the buttons below to manage this ticket.",
        embed=embed,
        view=view,
    )

    if pin_message:
        try:
            await message.pin()
        except Exception as pin_error:
            logger.warning(f"Failed to pin ticket message in thread {thread.id}: {pin_error}")

    logger.info(f"Created thread {thread.id} for ticket #{ticket.ticket_number}")
    return thread


async def publish_new_ticket(bot: discord.Client, guild: discord.Guild | None, ticket: Ticket) -> None:
    """Give a newly created ticket its team thread and put it on the dashboard.

    ``ticket.team`` must be loaded. Thread failures are logged, not raised: the
    ticket already exists and still belongs on the dashboard.
    """
    from bot.ticket_dashboard import trigger_dashboard

    if guild is None:
        logger.warning(f"No guild available; ticket {ticket.ticket_number} will have no thread")
    else:
        try:
            await create_ticket_thread(guild=guild, ticket=ticket, team=ticket.team, pin_message=True)
        except Exception:
            logger.exception(f"Failed to create thread for ticket {ticket.ticket_number}")
    trigger_dashboard(bot)
