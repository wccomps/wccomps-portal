"""Discord channel and role management using Django ORM."""

import logging
import re
from collections.abc import Awaitable, Collection, Sequence

import discord
from discord.ext import commands
from django.conf import settings
from django.utils import timezone

from bot.cogs.help_panels import HelpPanelsCog, TicketButton, _has_button
from bot.role_sync import sync_member_roles
from bot.utils import announcement as announcement_text
from bot.utils import (
    category_chat_channel,
    find_team_category,
    find_team_role,
    send_parts,
    team_category_name,
    team_chat_channel,
    team_role_name,
)
from core.models import AuditLog, QueuedAnnouncement
from team.models import Team
from ticketing.models import Ticket

logger = logging.getLogger(__name__)


async def delete_team_infrastructure(guild: discord.Guild, team: Team, reason: str) -> list[str]:
    """Delete a team's Discord category, channels and role and clear their IDs; returns which of the category and
    role it deleted. Deleting the chat channel deletes its ticket threads, so their IDs are cleared too."""
    deleted_items: list[str] = []

    category = find_team_category(guild, team)
    if category:
        chat_channel = team_chat_channel(guild, team)
        chat_deleted = False
        for channel in category.channels:
            try:
                await channel.delete(reason=reason)
            except Exception as e:
                logger.exception(f"Failed to delete channel {channel.name}: {e}")
            else:
                chat_deleted = chat_deleted or channel == chat_channel
        if chat_deleted:
            await Ticket.objects.filter(team=team, discord_thread_id__isnull=False).aupdate(discord_thread_id=None)

        try:
            await category.delete(reason=reason)
            deleted_items.append("category")
        except Exception as e:
            logger.exception(f"Failed to delete category: {e}")

    role = find_team_role(guild, team)
    if role:
        try:
            await role.delete(reason=reason)
            deleted_items.append("role")
        except Exception as e:
            logger.exception(f"Failed to delete role: {e}")

    team.discord_role_id = None
    team.discord_category_id = None
    await team.asave(update_fields=["discord_role_id", "discord_category_id"])

    return deleted_items


async def unlink_team_members(
    guild: discord.Guild,
    team: Team,
    admin_user: str,
    audit_reason: str,
) -> int:
    """Unlink all active Discord members from a team.

    Deactivates the DiscordLinks, syncs each member's roles so they lose what the link granted, and
    creates per-member audit log entries. Returns count of unlinked members.
    """
    from bot.role_sync import sync_member_roles

    members = [m async for m in team.members.filter(is_active=True).select_related("user")]
    unlinked_count = 0

    for link in members:
        member = guild.get_member(link.discord_id)

        link.is_active = False
        link.unlinked_at = timezone.now()
        await link.asave()
        unlinked_count += 1

        if member:
            await sync_member_roles(guild, member)

        await AuditLog.objects.acreate(
            action="user_unlinked",
            admin_user=admin_user,
            target_entity="discord_link",
            target_id=link.discord_id,
            details={
                "discord_username": str(member) if member else f"ID:{link.discord_id}",
                "team_name": team.team_name,
                "authentik_username": link.user.username,
                "reason": audit_reason,
            },
        )

    return unlinked_count


class TeamInfrastructureError(Exception):
    """A step of setting up a team's Discord role, category or channels failed; the message names the step."""


_TEAM_ROLE_NAME_PATTERN = re.compile(r"^Team (\d+)$")
_TEAM_CATEGORY_NAME_PATTERN = re.compile(r"^team (\d+)$", re.IGNORECASE)

_TALKS = {"read_messages": True, "send_messages": True, "connect": True, "speak": True}
# Roles found by name that see every team category, and what they may do there
GUEST_ROLE_PERMISSIONS: dict[str, dict[str, bool]] = {
    "WCComps Observers": {"read_messages": True, "send_messages": False, "connect": False},
    "Orange Team Guest": _TALKS,
    "Black Team Guest": _TALKS,
    "WCComps Room Judge": {"read_messages": True},
    "WCComps Operations Team": _TALKS,
    "WRCCDC Server Owners": _TALKS,
}

type Overwrites = dict[discord.Role | discord.Member | discord.Object, discord.PermissionOverwrite]


async def recreate_team_infrastructure(
    guild: discord.Guild, team: Team, reason: str, bot: discord.Client | None = None
) -> tuple[list[str], discord.CategoryChannel]:
    """Delete what the team has in Discord and set it up again; returns what was deleted and the new category."""
    old = {"role": find_team_role(guild, team), "category": find_team_category(guild, team)}
    deleted = await delete_team_infrastructure(guild, team, reason=reason)
    gone = {item.id for kind, item in old.items() if item and kind in deleted}
    return deleted, await setup_team_infrastructure(guild, team, bot=bot, gone=gone)


async def setup_team_infrastructure(
    guild: discord.Guild, team: Team, bot: discord.Client | None = None, *, gone: Collection[int] = ()
) -> discord.CategoryChannel:
    """Give a team its role and its category with chat and voice channels, reusing what exists (never the IDs in
    gone) and recording each ID once created; then deliver its queued announcements and sync its members' roles.

    When a reused category had no team role entry, it and each of its channels without one get one; no other entry
    is touched. Raises TeamInfrastructureError naming the step that failed."""
    team_number = team.team_number

    async def step[T](what: str, work: Awaitable[T]) -> T:
        try:
            return await work
        except discord.HTTPException as e:
            raise TeamInfrastructureError(f"Could not {what} for team {team_number}: {e}") from e

    role = find_team_role(guild, team, gone)
    if role is None:
        role = await step("create the role", _create_role(guild, team_number))
    await _record(team, discord_role_id=role.id)
    await step("position the role", _place(role, guild.roles, _TEAM_ROLE_NAME_PATTERN, team_number))

    overwrites = _category_overwrites(guild, role)
    category = find_team_category(guild, team, gone)
    if category is None:
        category = await step(
            "create the category",
            guild.create_category(
                name=team_category_name(team_number), overwrites=overwrites, reason="WCComps team category"
            ),
        )
    elif role not in category.overwrites and role != guild.default_role:
        # Channels first (Discord doesn't sync them from the category), so a retry redoes a failed one
        for channel in category.channels:
            if isinstance(channel, discord.TextChannel | discord.VoiceChannel) and role not in channel.overwrites:
                await step(
                    f"set {channel.name}'s permissions", channel.set_permissions(role, overwrite=overwrites[role])
                )
        await step("set the category's permissions", category.set_permissions(role, overwrite=overwrites[role]))
    await _record(team, discord_category_id=category.id)
    await step("position the category", _place(category, guild.categories, _TEAM_CATEGORY_NAME_PATTERN, team_number))

    chat_channel = category_chat_channel(category)
    if not chat_channel:
        chat_channel = await step(
            "create the chat channel",
            category.create_text_channel(f"team{team_number:02d}-chat", reason="WCComps team text channel"),
        )
    await step("post the ticket panel", _post_ticket_panel(bot, team_number, chat_channel))
    if not any(isinstance(c, discord.VoiceChannel) for c in category.channels):
        await step(
            "create the voice channel",
            category.create_voice_channel(f"team{team_number:02d}-voice", reason="WCComps team voice channel"),
        )
    logger.info(f"Set up infrastructure for {team.team_name}")

    await _deliver_queued_announcements(team, chat_channel)

    if team.is_active:
        async for link in team.members.filter(is_active=True):
            member = guild.get_member(link.discord_id)
            if member:
                await sync_member_roles(guild, member)

    return category


async def _record(team: Team, **ids: int) -> None:
    changed = [name for name, value in ids.items() if getattr(team, name) != value]
    for name in changed:
        setattr(team, name, ids[name])
    if changed:
        await team.asave(update_fields=changed)


async def _create_role(guild: discord.Guild, team_number: int) -> discord.Role:
    template_role = discord.utils.get(guild.roles, name=team_role_name(1))
    role = await guild.create_role(
        name=team_role_name(team_number),
        color=template_role.color if template_role else discord.Color.default(),
        mentionable=True,
        reason="WCComps team role",
    )
    logger.info(f"Created role {role.name}")
    return role


async def _place(
    item: discord.Role | discord.CategoryChannel,
    siblings: Sequence[discord.Role] | Sequence[discord.CategoryChannel],
    pattern: re.Pattern[str],
    team_number: int,
) -> None:
    """Move a team role or category after the closest lower numbered team's, else before the lowest one, unless it is
    there already."""
    numbered = sorted(
        [(int(match.group(1)), s) for s in siblings if s.id != item.id and (match := pattern.match(s.name))],
        key=lambda pair: pair[0],
    )
    lower = [s for number, s in numbered if number < team_number]
    if lower:
        if item.position != lower[-1].position + 1:
            await item.edit(position=lower[-1].position + 1)
    elif numbered and item.position != numbered[0][1].position - 1:
        await item.edit(position=numbered[0][1].position)


def _category_overwrites(guild: discord.Guild, role: discord.Role) -> Overwrites:
    overwrites: Overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False, send_messages=False, connect=False),
        role: discord.PermissionOverwrite(
            read_messages=True,
            send_messages=True,
            connect=True,
            speak=True,
            embed_links=True,
            attach_files=True,
        ),
    }
    by_id = [
        (settings.WHITETEAM_ROLE_ID, {"read_messages": True, "connect": True}),
        (settings.ORANGETEAM_ROLE_ID, _TALKS),
    ]
    for role_id, permissions in by_id:
        staff_role = guild.get_role(role_id)
        if staff_role and staff_role not in (guild.default_role, role):
            overwrites[staff_role] = discord.PermissionOverwrite(**permissions)
    for name, permissions in GUEST_ROLE_PERMISSIONS.items():
        if guest_role := discord.utils.get(guild.roles, name=name):
            overwrites[guest_role] = discord.PermissionOverwrite(**permissions)
    return overwrites


async def _post_ticket_panel(bot: discord.Client | None, team_number: int, chat_channel: discord.TextChannel) -> None:
    """Post the team's ticket panel unless the bot's is among the chat channel's first messages."""
    help_panels_cog = bot.get_cog("HelpPanelsCog") if isinstance(bot, commands.Bot) else None
    if not isinstance(help_panels_cog, HelpPanelsCog):
        return
    async for message in chat_channel.history(limit=50, oldest_first=True):
        if bot and message.author == bot.user and _has_button(message, TicketButton.CUSTOM_ID):
            return
    await help_panels_cog.post_team_ticket_panel(chat_channel.id)
    logger.info(f"Posted ticket panel to team {team_number} channel")


async def _deliver_queued_announcements(team: Team, channel: discord.TextChannel) -> None:
    announcements = [
        a async for a in QueuedAnnouncement.objects.filter(team=team, delivered_at__isnull=True).order_by("created_at")
    ]
    delivered = 0
    for queued in announcements:
        try:
            await send_parts(channel, announcement_text(queued.sender_name, queued.message))
            queued.delivered_at = timezone.now()
            await queued.asave()
            delivered += 1
        except Exception as e:
            logger.warning(f"Failed to deliver queued announcement {queued.id}: {e}")
    if delivered:
        logger.info(f"Delivered {delivered} queued announcement(s) to team {team.team_number}")
