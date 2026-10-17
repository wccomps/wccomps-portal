"""Discord channel and role management using Django ORM."""

import logging
import re

import discord
from django.conf import settings
from django.utils import timezone

from bot.utils import announcement as announcement_text
from bot.utils import send_parts
from core.models import AuditLog
from team.models import Team

logger = logging.getLogger(__name__)


async def delete_team_infrastructure(guild: discord.Guild, team: Team, reason: str) -> list[str]:
    """Delete a team's Discord category, channels, and role. Clears DB IDs.

    Returns list of deleted item types (e.g. ["category", "role"]).
    """
    deleted_items: list[str] = []

    if team.discord_category_id:
        category = guild.get_channel(team.discord_category_id)
        if category and isinstance(category, discord.CategoryChannel):
            for channel in category.channels:
                try:
                    await channel.delete(reason=reason)
                except Exception as e:
                    logger.exception(f"Failed to delete channel {channel.name}: {e}")

            try:
                await category.delete(reason=reason)
                deleted_items.append("category")
            except Exception as e:
                logger.exception(f"Failed to delete category: {e}")

    if team.discord_role_id:
        role = guild.get_role(team.discord_role_id)
        if role:
            try:
                await role.delete(reason=reason)
                deleted_items.append("role")
            except Exception as e:
                logger.exception(f"Failed to delete role: {e}")

    team.discord_role_id = None
    team.discord_category_id = None
    await team.asave()

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


class DiscordManager:
    """Manages Discord roles and channels for teams."""

    def __init__(
        self,
        guild: discord.Guild,
        bot: discord.Client | discord.ext.commands.Bot | None = None,
    ):
        self.guild = guild
        self.bot = bot

    async def setup_team_infrastructure(
        self, team_number: int
    ) -> tuple[discord.Role | None, discord.CategoryChannel | None]:
        """
        Set up a team's Discord role and category, creating whichever is missing, and record their IDs.

        Safe to run again: an existing role or category is reused as-is (its channels and
        permissions aren't repaired).
        """
        team = await Team.objects.filter(team_number=team_number).afirst()
        if not team:
            logger.error(f"Team {team_number} not found in database")
            return None, None

        role = await self._create_or_get_role(team_number)
        if not role:
            return None, None

        category = await self._create_team_category(team_number, role)

        if category:
            team.discord_role_id = role.id
            team.discord_category_id = category.id
            await team.asave()
            logger.info(f"Set up infrastructure for {team.team_name}")
        elif role:
            team.discord_role_id = role.id
            await team.asave()
            logger.warning(f"Only role created for {team.team_name}, category failed")
        elif category:
            team.discord_category_id = category.id
            await team.asave()
            logger.warning(f"Only category created for {team.team_name}, role failed")

        return role, category

    async def _create_or_get_role(self, team_number: int) -> discord.Role | None:
        """Create or get existing team role with format 'Team XX'."""
        role_name = f"Team {team_number:02d}"

        # Check if role already exists (check both with and without zero-padding)
        role = discord.utils.get(self.guild.roles, name=role_name)
        if not role:
            alt_role_name = f"Team {team_number}"
            role = discord.utils.get(self.guild.roles, name=alt_role_name)

        if role:
            logger.info(f"Role {role.name} already exists")
            return role

        # Get Team 01 role to copy color
        template_role = discord.utils.get(self.guild.roles, name="Team 01")
        role_color = template_role.color if template_role else discord.Color.default()

        # Find position: insert after the closest existing team number
        team_roles = []
        for r in self.guild.roles:
            match = re.match(r"^Team (\d+)$", r.name)
            if match:
                team_roles.append((int(match.group(1)), r))

        team_roles.sort(key=lambda x: x[0], reverse=True)

        position = None
        for num, r in team_roles:
            if num < team_number:
                position = r.position
                break

        try:
            role = await self.guild.create_role(
                name=role_name,
                color=role_color,
                mentionable=True,
                reason="WCComps team role",
            )

            if position is not None:
                await role.edit(position=position + 1)
                logger.info(f"Created role {role_name} at position {position + 1}")
            elif team_roles:
                # No lower-numbered role found, but other team roles exist
                lowest_role = team_roles[-1][1]
                await role.edit(position=lowest_role.position)
                logger.info(f"Created role {role_name} before {lowest_role.name}")
            else:
                logger.info(f"Created role {role_name}")

            return role
        except discord.errors.Forbidden:
            logger.exception(f"No permission to create role {role_name}")
            return None

    async def _create_team_category(self, team_number: int, role: discord.Role) -> discord.CategoryChannel | None:
        """Create code-defined team category with channels (idempotent)."""
        category_name = f"team {team_number:02d}"

        # Reuse an existing category (either name style) rather than creating a duplicate
        existing_category = discord.utils.get(self.guild.categories, name=category_name)
        if not existing_category:
            alt_category_name = f"team {team_number}"
            existing_category = discord.utils.get(self.guild.categories, name=alt_category_name)

        if existing_category:
            logger.info(f"Category {existing_category.name} already exists")
            return existing_category

        try:
            overwrites: dict[discord.Role | discord.Member | discord.Object, discord.PermissionOverwrite] = {}

            white_team = self.guild.get_role(settings.WHITETEAM_ROLE_ID)
            observers = discord.utils.get(self.guild.roles, name="WCComps Observers")
            orange_team = self.guild.get_role(settings.ORANGETEAM_ROLE_ID)
            orange_team_guest = discord.utils.get(self.guild.roles, name="Orange Team Guest")
            black_team_guest = discord.utils.get(self.guild.roles, name="Black Team Guest")
            room_judge = discord.utils.get(self.guild.roles, name="WCComps Room Judge")
            operations_team = discord.utils.get(self.guild.roles, name="WCComps Operations Team")
            server_owners = discord.utils.get(self.guild.roles, name="WRCCDC Server Owners")

            overwrites[self.guild.default_role] = discord.PermissionOverwrite(
                read_messages=False, send_messages=False, connect=False
            )

            overwrites[role] = discord.PermissionOverwrite(
                read_messages=True,
                send_messages=True,
                connect=True,
                speak=True,
                embed_links=True,
                attach_files=True,
            )

            if white_team:
                overwrites[white_team] = discord.PermissionOverwrite(read_messages=True, connect=True)

            if observers:
                overwrites[observers] = discord.PermissionOverwrite(
                    read_messages=True, send_messages=False, connect=False
                )

            if orange_team:
                overwrites[orange_team] = discord.PermissionOverwrite(
                    read_messages=True, send_messages=True, connect=True, speak=True
                )

            if orange_team_guest:
                overwrites[orange_team_guest] = discord.PermissionOverwrite(
                    read_messages=True, send_messages=True, connect=True, speak=True
                )

            if black_team_guest:
                overwrites[black_team_guest] = discord.PermissionOverwrite(
                    read_messages=True, send_messages=True, connect=True, speak=True
                )

            if room_judge:
                overwrites[room_judge] = discord.PermissionOverwrite(read_messages=True)

            if operations_team:
                overwrites[operations_team] = discord.PermissionOverwrite(
                    read_messages=True, send_messages=True, connect=True, speak=True
                )

            if server_owners:
                overwrites[server_owners] = discord.PermissionOverwrite(
                    read_messages=True, send_messages=True, connect=True, speak=True
                )

            category = await self.guild.create_category(
                name=category_name,
                overwrites=overwrites,
                reason="WCComps team category",
            )

            text_channel = await category.create_text_channel(
                f"team{team_number:02d}-chat", reason="WCComps team text channel"
            )
            await category.create_voice_channel(f"team{team_number:02d}-voice", reason="WCComps team voice channel")

            # Position category relative to other team categories (excluding the one we just created)
            other_team_categories = []
            for cat in self.guild.categories:
                if cat.id == category.id:
                    continue
                match = re.match(r"^team (\d+)$", cat.name, re.IGNORECASE)
                if match:
                    other_team_categories.append((int(match.group(1)), cat))

            if other_team_categories:
                other_team_categories.sort(key=lambda x: x[0])

                # Find position: after the closest lower-numbered team, or before the first higher-numbered team
                positioned = False
                for num, cat in reversed(other_team_categories):
                    if num < team_number:
                        await category.edit(position=cat.position + 1)
                        logger.info(f"Positioned after team {num}")
                        positioned = True
                        break

                if not positioned:
                    # No lower-numbered team exists, position before the lowest-numbered team
                    first_team_cat = other_team_categories[0][1]
                    await category.edit(position=first_team_cat.position)
                    logger.info(f"Positioned before team {other_team_categories[0][0]} (no lower-numbered team)")

            logger.info(f"Created code-defined category for Team {team_number}")

            if self.bot and hasattr(self.bot, "get_cog"):
                try:
                    from bot.cogs.help_panels import HelpPanelsCog

                    help_panels_cog = self.bot.get_cog("HelpPanelsCog")
                    if isinstance(help_panels_cog, HelpPanelsCog):
                        await help_panels_cog.post_team_ticket_panel(text_channel.id)
                        logger.info(f"Posted ticket panel to team {team_number} channel")
                except Exception as e:
                    logger.warning(f"Could not post ticket panel to team channel: {e}")

            await self._deliver_queued_announcements(team_number, text_channel)

            return category

        except discord.errors.Forbidden:
            logger.exception(f"No permission to create category for Team {team_number}")
            return None
        except Exception as e:
            logger.exception(f"Error creating category: {e}")
            return None

    async def _deliver_queued_announcements(self, team_number: int, channel: discord.TextChannel) -> int:
        from django.utils import timezone

        from core.models import QueuedAnnouncement

        team = await Team.objects.filter(team_number=team_number).afirst()
        if not team:
            return 0

        announcements = [
            a
            async for a in QueuedAnnouncement.objects.filter(team=team, delivered_at__isnull=True).order_by(
                "created_at"
            )
        ]

        delivered_count = 0
        for announcement in announcements:
            try:
                await send_parts(channel, announcement_text(announcement.sender_name, announcement.message))
                announcement.delivered_at = timezone.now()
                await announcement.asave()
                delivered_count += 1
            except Exception as e:
                logger.warning(f"Failed to deliver queued announcement {announcement.id}: {e}")

        if delivered_count > 0:
            logger.info(f"Delivered {delivered_count} queued announcement(s) to team {team_number}")

        return delivered_count
