"""Permission checks for Discord bot commands.

A Discord user holds the permissions of the portal account their active DiscordLink points at, decided by
core.auth_utils.groups_grant as on the web. Team commands need more: an enabled seat on the team (DiscordLink.seat).
"""

import discord
from asgiref.sync import sync_to_async
from discord.app_commands.commands import Check

from core.auth_utils import groups_grant, permission_groups_of
from core.permission_constants import PERMISSION_MAP
from team.models import DiscordLink, Seat, Team
from ticketing.access import TICKET_STAFF, ticket_open_to
from ticketing.models import Ticket


def _active_link(discord_id: int) -> DiscordLink | None:
    return (
        DiscordLink.objects.select_related("user__usergroups", "team")
        .filter(discord_id=discord_id, is_active=True)
        .first()
    )


def _enabled_seat(link: DiscordLink | None) -> Seat | None:
    seat = link.seat() if link else None
    return seat if seat and seat.enabled else None


@sync_to_async
def has_permission(discord_id: int, permission: str) -> bool:
    link = _active_link(discord_id)
    return link is not None and groups_grant(permission_groups_of(link.user), permission)


@sync_to_async
def linked_team_member(discord_id: int) -> Seat | None:
    """The enabled seat of the linked account; None if unlinked, on no team or disabled."""
    return _enabled_seat(_active_link(discord_id))


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


NOT_ON_A_TEAM = (
    "Blue Team membership required.\n\n"
    "You must be linked to a competition team to use this command.\n"
    "Use `/link` to connect your Discord account to your team."
)
ACCOUNT_DISABLED = "Your team's account is disabled right now, so team commands are closed until it is enabled again."


@sync_to_async
def _blue_team_refusal(discord_id: int) -> str | None:
    link = _active_link(discord_id)
    match link.seat() if link else None:
        case Seat(enabled=True):
            return None
        case Seat(enabled=False):
            return ACCOUNT_DISABLED
        case _:
            return NOT_ON_A_TEAM


async def check_blue_team(interaction: discord.Interaction) -> bool:
    refusal = await _blue_team_refusal(interaction.user.id)
    if refusal is None:
        return True
    await interaction.response.send_message(refusal, ephemeral=True)
    return False


@sync_to_async
def ticket_access(discord_id: int) -> tuple[bool, Team | None]:
    """Whether the linked account is ticket staff, and the team of its enabled seat, from one read of the link."""
    link = _active_link(discord_id)
    if link is None:
        return False, None
    seat = _enabled_seat(link)
    return groups_grant(permission_groups_of(link.user), TICKET_STAFF), seat.team if seat else None


async def may_write_to_ticket(ticket: Ticket, discord_id: int) -> bool:
    """ticketing.access.ticket_open_to for a Discord user, whose team is their link's enabled seat."""
    staff, team = await ticket_access(discord_id)
    return ticket_open_to(ticket, staff=staff, team=team)
