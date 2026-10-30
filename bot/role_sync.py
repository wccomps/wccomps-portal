"""Discord roles in the competition guild, kept in line with Authentik groups and team seats.

The synced roles are the ones mapped from Authentik groups (GROUP_ROLE_MAPPING), the teams' roles and
Blueteam. Each belongs to exactly the members whose active DiscordLink grants it; everyone else holding it
loses it, including members who haven't linked. Bots, and roles the bot can't manage, are left alone.
"""

import asyncio
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass

import discord
from asgiref.sync import sync_to_async
from django.conf import settings

from core.discord_tasks import SyncRolesResult
from core.utils import role_sync_summary

logger = logging.getLogger(__name__)

GUILD_CHUNK_TIMEOUT = 30.0


@dataclass(frozen=True)
class RoleGrants:
    """Synced role IDs, and the ones each linked Discord user should hold."""

    managed: frozenset[int]
    by_member: dict[int, frozenset[int]]


def role_grants(discord_ids: set[int] | None = None) -> RoleGrants:
    """What active DiscordLinks grant: the role mapped from each of the account's Authentik groups, and
    for a seat on an active team, that team's role and Blueteam. Limited to discord_ids when given."""
    from team.models import DiscordLink, Team

    mapped = settings.GROUP_ROLE_MAPPING
    team_roles = set(Team.objects.exclude(discord_role_id=None).values_list("discord_role_id", flat=True))
    managed = frozenset(role_id for role_id in {*mapped.values(), *team_roles, settings.BLUETEAM_ROLE_ID} if role_id)

    links = DiscordLink.objects.filter(is_active=True).select_related("user__usergroups", "team")
    if discord_ids is not None:
        links = links.filter(discord_id__in=discord_ids)
    by_member: dict[int, frozenset[int]] = {}
    for link in links:
        user_groups = getattr(link.user, "usergroups", None)
        groups = set(user_groups.permission_groups) if user_groups else set()
        roles = [role_id for group, role_id in mapped.items() if group in groups]
        if link.team and link.team.is_active:
            roles += [link.team.discord_role_id or 0, settings.BLUETEAM_ROLE_ID]
        by_member[link.discord_id] = frozenset(role_id for role_id in roles if role_id)
    return RoleGrants(managed, by_member)


def competition_guild(bot: discord.Client) -> discord.Guild | None:
    guild = bot.get_guild(settings.COMPETITION_GUILD_ID)
    if not guild:
        logger.error(f"Competition guild {settings.COMPETITION_GUILD_ID} not found")
    return guild


async def sync_roles(guild: discord.Guild, *, dry_run: bool = False) -> SyncRolesResult:
    """Give every member in the guild's cache the synced roles granted to them and take away the ones not."""
    if not guild.chunked:
        chunk_start = time.time()
        try:
            await asyncio.wait_for(guild.chunk(), timeout=GUILD_CHUNK_TIMEOUT)
        except TimeoutError:
            logger.warning(
                f"Guild chunk timed out after {time.time() - chunk_start:.2f}s, "
                f"using cached members ({len(guild.members)} available)"
            )
    stats = await _reconcile(guild, guild.members, dry_run=dry_run, recheck=True)
    summary = role_sync_summary(stats, dry_run=dry_run)
    if dry_run or stats["roles_added"] or stats["roles_removed"] or stats["errors"]:
        logger.info(summary)
    else:
        logger.debug(summary)
    return stats


async def sync_member_roles(guild: discord.Guild, member: discord.Member, *, dry_run: bool = False) -> SyncRolesResult:
    """Sync one member, e.g. right after they link or unlink, instead of waiting for the next full pass."""
    return await _reconcile(guild, [member], dry_run=dry_run, recheck=False)


async def _reconcile(
    guild: discord.Guild, members: Sequence[discord.Member], *, dry_run: bool, recheck: bool
) -> SyncRolesResult:
    stats: SyncRolesResult = {"roles_added": 0, "roles_removed": 0, "errors": 0, "changes": []}
    grants = await sync_to_async(role_grants)(None if recheck else {m.id for m in members})
    roles = _manageable_roles(guild, grants.managed, stats)
    prefix = "[DRY RUN] " if dry_run else ""

    for member in members:
        if member.bot:
            continue
        granted = grants.by_member.get(member.id)
        add, remove = _changes(member, roles, granted or frozenset())
        if not add and not remove:
            continue
        if recheck:
            # A whole-guild pass works from a snapshot; re-read this member's grant so a link made or
            # ended since the pass began isn't undone
            granted = (await sync_to_async(role_grants)({member.id})).by_member.get(member.id)
            add, remove = _changes(member, roles, granted or frozenset())
        who = f"{member.name} ({member.display_name})"
        reason = "not granted by their Authentik groups or team seat" if granted is not None else "not linked"
        try:
            if add and not dry_run:
                await member.add_roles(*add, reason="Authentik sync: granted")
            if remove and not dry_run:
                await member.remove_roles(*remove, reason=f"Authentik sync: {reason}")
        except discord.HTTPException as e:
            logger.warning(f"Could not update roles for {who}: {e}")
            stats["errors"] += 1
            stats["changes"].append(f"Could not update roles for {who}: {e}")
            continue
        stats["roles_added"] += len(add)
        stats["roles_removed"] += len(remove)
        stats["changes"] += [f"{prefix}Added {role.name} to {who}" for role in add]
        stats["changes"] += [f"{prefix}Removed {role.name} from {who} ({reason})" for role in remove]
    return stats


def _manageable_roles(
    guild: discord.Guild, role_ids: frozenset[int], stats: SyncRolesResult
) -> dict[int, discord.Role]:
    """The synced roles the bot can assign; each problem is reported once rather than failing per member."""
    if not guild.me.guild_permissions.manage_roles:
        _report(stats, "Bot lacks the Manage Roles permission, so no roles were synced")
        return {}
    roles: dict[int, discord.Role] = {}
    for role_id in sorted(role_ids):
        role = guild.get_role(role_id)
        if role is None:
            _report(stats, f"Role {role_id} is configured but not in the guild")
        elif not role.is_assignable():
            _report(
                stats,
                f"Bot can't assign {role.name}: it is @everyone, managed by an integration, "
                "or at or above the bot's top role",
            )
        else:
            roles[role_id] = role
    return roles


def _report(stats: SyncRolesResult, problem: str) -> None:
    logger.warning(problem)
    stats["errors"] += 1
    stats["changes"].append(problem)


def _changes(
    member: discord.Member, roles: dict[int, discord.Role], granted: frozenset[int]
) -> tuple[list[discord.Role], list[discord.Role]]:
    held = {role.id for role in member.roles}
    add = [role for role_id, role in roles.items() if role_id in granted and role_id not in held]
    remove = [role for role_id, role in roles.items() if role_id not in granted and role_id in held]
    return add, remove
