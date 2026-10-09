"""Shared competition actions for commands and timer."""

import logging
import re
from collections.abc import Callable

import discord
from asgiref.sync import sync_to_async
from django.db import connection
from django.utils import timezone
from scoring.models import QuotientMetadataCache

from bot.utils import log_to_ops_channel
from core.models import AuditLog, CompetitionConfig, QueuedAnnouncement
from core.services.competition import CompetitionRunResult, run_competition_to_completion
from team.models import MAX_TEAMS, DiscordLink, Team

logger = logging.getLogger(__name__)


async def run_competition(enable: bool, actor: str, on_step: Callable[[], None] | None = None) -> CompetitionRunResult:
    """Start (enable=True) or stop the competition via the shared service, off the event loop.

    Not thread_sensitive: the run makes dozens of Authentik calls and would otherwise hold the
    single thread every other sync_to_async call in the bot waits on.
    """
    return await sync_to_async(_run_and_close, thread_sensitive=False)(enable, actor, on_step)


def _run_and_close(enable: bool, actor: str, on_step: Callable[[], None] | None) -> CompetitionRunResult:
    # A pool thread keeps its DB connection, and recycle_db_connection only reaches the bot's main ORM
    # thread; close it so a connection broken between runs can't fail the next start or stop.
    try:
        return run_competition_to_completion(enable, actor, on_step)
    finally:
        connection.close()


async def update_status_channel(bot: discord.Client) -> bool:
    """Edit (or post) the competition status message; False if there is no status channel or it fails."""
    config = await sync_to_async(CompetitionConfig.get_config)()

    if not config.status_channel_id:
        return False

    channel = bot.get_channel(config.status_channel_id)
    if not channel or not isinstance(channel, discord.TextChannel):
        logger.warning(f"Status channel {config.status_channel_id} not found or not a text channel")
        return False

    embed = _build_status_embed(config)

    try:
        if config.status_message_id:
            try:
                message = await channel.fetch_message(config.status_message_id)
                await message.edit(embed=embed)
                return True
            except discord.NotFound:
                logger.info("Status message not found, creating new one")

        message = await channel.send(embed=embed)

        @sync_to_async
        def save_message_id() -> None:
            config.status_message_id = message.id
            config.save(update_fields=["status_message_id"])

        await save_message_id()
        return True

    except Exception as e:
        logger.exception(f"Failed to update status channel: {e}")
        return False


def _build_status_embed(config: CompetitionConfig) -> discord.Embed:
    if config.applications_enabled:
        status = "RUNNING"
        color = discord.Color.green()
    elif config.competition_start_time:
        status = "SCHEDULED"
        color = discord.Color.blue()
    else:
        status = "STOPPED"
        color = discord.Color.red()

    embed = discord.Embed(
        title="Competition Status",
        description=f"**{status}**",
        color=color,
    )

    if config.competition_start_time:
        embed.add_field(
            name="Scheduled Start",
            value=f"<t:{int(config.competition_start_time.timestamp())}:F>",
            inline=True,
        )

    if config.competition_end_time:
        embed.add_field(
            name="Scheduled End",
            value=f"<t:{int(config.competition_end_time.timestamp())}:F>",
            inline=True,
        )

    if config.controlled_applications:
        apps_str = ", ".join(config.controlled_applications)
        embed.add_field(
            name="Controlled Applications",
            value=apps_str,
            inline=False,
        )

    account_status = "Enabled" if config.applications_enabled else "Disabled"
    embed.add_field(
        name="Team Accounts",
        value=f"{account_status} ({MAX_TEAMS} teams)",
        inline=True,
    )

    embed.set_footer(text="Last updated")
    embed.timestamp = discord.utils.utcnow()

    return embed


async def run_competition_cleanup(bot: discord.Client, guild: discord.Guild, actor: str) -> None:
    """Tear down the competition in Discord and the database; progress goes to the ops channel.

    The one implementation behind /competition cleanup-competition and the ops page's cleanup
    (queued as a cleanup_competition task). Never raises: failures are reported to ops.
    """
    config = await sync_to_async(CompetitionConfig.get_config)()
    if config.applications_enabled:
        await log_to_ops_channel(bot, "Cleanup refused: the competition is still running")
        return

    try:
        await log_to_ops_channel(bot, f"Competition Cleanup Started by {actor}")

        # Deactivate team member links only (preserve admin/support links)
        links_to_deactivate = [
            link
            async for link in DiscordLink.objects.filter(is_active=True, team__isnull=False).select_related(
                "team", "user"
            )
        ]

        deactivated = 0
        for link in links_to_deactivate:
            link.is_active = False
            link.unlinked_at = timezone.now()
            await link.asave()
            deactivated += 1

            await AuditLog.objects.acreate(
                action="user_unlinked",
                admin_user=actor,
                target_entity="discord_link",
                target_id=link.discord_id,
                details={
                    "discord_id": link.discord_id,
                    "team_name": link.team.team_name if link.team else "Unknown",
                    "authentik_username": link.user.username,
                    "reason": "competition_cleanup",
                },
            )

        await log_to_ops_channel(bot, f"Deactivated {deactivated} team member links")

        deleted_count = 0
        for category in guild.categories:
            match = re.match(r"^team\s*(\d+)$", category.name, re.IGNORECASE)
            if match:
                try:
                    for channel in category.channels:
                        await channel.delete(reason="Competition cleanup")
                    await category.delete(reason="Competition cleanup")
                    deleted_count += 1
                    logger.info(f"Deleted {category.name}")
                except Exception as e:
                    logger.exception(f"Failed to delete {category.name}: {e}")

        await log_to_ops_channel(bot, f"Deleted {deleted_count} team categories")

        from bot.role_sync import sync_roles

        # Before the role IDs are cleared: afterwards the sync no longer counts team roles as its own
        stats = await sync_roles(guild)
        await log_to_ops_channel(bot, f"Removed {stats['roles_removed']} roles from members who are no longer linked")

        await Team.objects.all().aupdate(discord_category_id=None, discord_role_id=None)

        active_helpers = [
            dl async for dl in DiscordLink.objects.filter(is_student_helper=True, is_active=True).select_related("user")
        ]

        helpers_removed = 0
        for discord_link in active_helpers:
            try:
                if discord_link.helper_role_id:
                    role = guild.get_role(discord_link.helper_role_id)
                    if role:
                        for member in guild.members:
                            if role in member.roles:
                                try:
                                    await member.remove_roles(role, reason="Competition cleanup")
                                except Exception as e:
                                    logger.warning(f"Could not remove helper role from {member}: {e}")

                discord_link.is_student_helper = False
                discord_link.helper_removal_reason = "Competition cleanup"
                discord_link.helper_deactivated_at = timezone.now()
                await discord_link.asave()
                helpers_removed += 1

                await AuditLog.objects.acreate(
                    action="helper_removed",
                    admin_user=actor,
                    target_entity="discordlink",
                    target_id=discord_link.id,
                    details={
                        "discord_id": discord_link.discord_id,
                        "discord_username": discord_link.discord_username,
                        "role_name": discord_link.helper_role_name,
                        "reason": "competition_cleanup",
                    },
                )
            except Exception as e:
                logger.exception(f"Error removing helper {discord_link.user.username}: {e}")

        if helpers_removed > 0:
            await log_to_ops_channel(bot, f"Removed {helpers_removed} student helper role(s)")

        helper_role_ids: set[int] = set()
        async for role_id in DiscordLink.objects.filter(helper_role_id__isnull=False).values_list(
            "helper_role_id", flat=True
        ):
            if role_id is not None:
                helper_role_ids.add(role_id)

        helper_role_removals = 0
        for role_id in helper_role_ids:
            role = guild.get_role(role_id)
            if role:
                for member in role.members:
                    try:
                        await member.remove_roles(role, reason="Competition cleanup")
                        helper_role_removals += 1
                    except Exception as e:
                        logger.warning(f"Could not remove {role.name} from {member}: {e}")

        room_judge_role = discord.utils.get(guild.roles, name="WCComps Room Judge")
        if room_judge_role:
            for member in room_judge_role.members:
                try:
                    await member.remove_roles(room_judge_role, reason="Competition cleanup")
                    helper_role_removals += 1
                except Exception as e:
                    logger.warning(f"Could not remove Room Judge from {member}: {e}")

        if helper_role_removals > 0:
            await log_to_ops_channel(bot, f"Removed helper/judge roles from {helper_role_removals} members")

        # The start message holds this event's credentials; the next event gets its own.
        await CompetitionConfig.objects.filter(pk=config.pk).aupdate(
            competition_start_time=None, competition_end_time=None, start_message=""
        )
        await QuotientMetadataCache.objects.all().adelete()

        deleted_announcements = await QueuedAnnouncement.objects.all().adelete()
        if deleted_announcements[0] > 0:
            await log_to_ops_channel(bot, f"Cleared {deleted_announcements[0]} queued announcements")

        await AuditLog.objects.acreate(
            action="competition_cleanup",
            admin_user=actor,
            target_entity="competition",
            target_id=0,
            details={
                "deactivated_links": deactivated,
                "deleted_categories": deleted_count,
                "removed_roles": stats["roles_removed"],
                "helpers_removed": helpers_removed,
            },
        )

        await log_to_ops_channel(
            bot,
            f"Competition Cleanup Complete\n"
            f"- Deactivated {deactivated} team links\n"
            f"- Deleted {deleted_count} team categories\n"
            f"- Removed {stats['roles_removed']} roles",
        )

        await update_status_channel(bot)

    except Exception as e:
        logger.exception(f"Cleanup error: {e}")
        await log_to_ops_channel(bot, f"Cleanup Error: {e}")
