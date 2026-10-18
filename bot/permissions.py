"""Permission checks for Discord bot commands.

A Discord user holds the stored Authentik groups (UserGroups) of the portal account their
active DiscordLink points at, read and interpreted exactly as the web portal reads them.
"""

from typing import NamedTuple

import discord
from asgiref.sync import sync_to_async
from discord.app_commands.commands import Check
from django.contrib.auth.models import User

from core.auth_utils import get_authentik_groups, team_for_groups
from core.permission_constants import PERMISSION_MAP, check_groups_for_permission
from team.models import DiscordLink, Team


class TeamMember(NamedTuple):
    user: User
    team: Team


def _linked_user(discord_id: int) -> User | None:
    link = DiscordLink.objects.select_related("user__usergroups").filter(discord_id=discord_id, is_active=True).first()
    return link.user if link else None


@sync_to_async
def _linked_groups(discord_id: int) -> list[str]:
    user = _linked_user(discord_id)
    return get_authentik_groups(user) if user else []


async def has_permission(discord_id: int, permission: str) -> bool:
    return check_groups_for_permission(await _linked_groups(discord_id), permission)


@sync_to_async
def linked_team_member(discord_id: int) -> TeamMember | None:
    """The linked account and the team its Authentik groups put it on; None if unlinked or on no team."""
    user = _linked_user(discord_id)
    team = team_for_groups(get_authentik_groups(user)) if user else None
    return TeamMember(user, team) if user and team else None


def permission_check(permission: str) -> Check:
    """An app_commands.check passing users whose groups grant `permission`; everyone else is told which groups do."""
    title = permission.replace("_", " ").capitalize()
    groups = ", ".join(f"`{group}`" for group in PERMISSION_MAP[permission])

    async def check(interaction: discord.Interaction) -> bool:
        if await has_permission(interaction.user.id, permission):
            return True
        await interaction.response.send_message(
            f"{title} permissions required.\n\n"
            f"You need one of these Authentik groups: {groups}.\n"
            "If you have one, link your account with `/link`.",
            ephemeral=True,
        )
        return False

    return check


async def check_blue_team(interaction: discord.Interaction) -> bool:
    if await linked_team_member(interaction.user.id):
        return True
    await interaction.response.send_message(
        "Blue Team membership required.\n\n"
        "You must be linked to a competition team to use this command.\n"
        "Use `/link` to connect your Discord account to your team.",
        ephemeral=True,
    )
    return False
