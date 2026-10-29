"""Shared competition actions for commands and timer."""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import discord
from asgiref.sync import sync_to_async
from django.conf import settings
from django.db import connection
from django.utils import timezone
from scoring.models import QuotientMetadataCache

from bot.discord_manager import delete_team_infrastructure
from bot.utils import (
    announcement,
    log_to_ops_channel,
    send_announcement,
    send_parts,
    team_chat_channel,
)
from core.broadcast import AllTeams, Announcements, BroadcastTarget, Teams
from core.models import AuditLog, CompetitionConfig, QueuedAnnouncement
from core.services.competition import CompetitionRunResult, run_competition_to_completion
from core.utils import shorten
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
    elif config.schedule_will_start():
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


async def _deactivate_team_links(actor: str) -> int:
    """Deactivate team member links only (admin and support links stay), auditing each."""
    links_to_deactivate = [
        link
        async for link in DiscordLink.objects.filter(is_active=True, team__isnull=False).select_related("team", "user")
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
    return deactivated


async def run_competition_cleanup(
    bot: discord.Client, guild: discord.Guild, actor: str, closed_out_event_id: int | None = None
) -> None:
    """Tear down the competition in Discord and the database; progress goes to the ops channel.

    The one implementation behind /competition cleanup-competition (the full cleanup) and Close Out Event (queued as
    a cleanup_competition task with closed_out_event_id). The close-out already deleted the team links and reset the
    database in its transaction, so for it this does only the Discord side, and skips if an event was opened or the
    competition started since. Never raises: failures are reported to ops.
    """
    config = await sync_to_async(CompetitionConfig.get_config)()
    if config.applications_enabled:
        await log_to_ops_channel(bot, "Cleanup refused: the competition is still running")
        return
    close_out = closed_out_event_id is not None
    if close_out:
        from registration.models import Event

        opened = await Event.objects.select_related("season").filter(is_active=True).afirst()
        if opened is not None:
            await log_to_ops_channel(
                bot,
                f"Discord cleanup after the close-out skipped: {opened} was opened since. Run "
                "/competition cleanup-competition if the old team channels and roles still need removing.",
            )
            return

    try:
        await log_to_ops_channel(bot, f"Competition Cleanup Started by {actor}")

        deactivated = 0
        if not close_out:
            deactivated = await _deactivate_team_links(actor)
            await log_to_ops_channel(bot, f"Deactivated {deactivated} team member links")

        from bot.role_sync import sync_roles

        # Before the role IDs are cleared: afterwards the sync no longer counts team roles as its own
        stats = await sync_roles(guild)
        await log_to_ops_channel(bot, f"Removed {stats['roles_removed']} roles from members who are no longer linked")

        deleted_count = 0
        async for team in Team.objects.all().order_by("team_number"):
            removed = await delete_team_infrastructure(guild, team, reason="Competition cleanup")
            if any(item.startswith("category") for item in removed):
                deleted_count += 1

        await log_to_ops_channel(bot, f"Deleted {deleted_count} team categories")

        await Team.objects.all().aupdate(discord_category_id=None, discord_role_id=None)

        judges_removed = 0
        room_judge_role = discord.utils.get(guild.roles, name="WCComps Room Judge")
        if room_judge_role:
            for member in room_judge_role.members:
                try:
                    await member.remove_roles(room_judge_role, reason="Competition cleanup")
                    judges_removed += 1
                except Exception as e:
                    logger.warning(f"Could not remove Room Judge from {member}: {e}")

        if judges_removed > 0:
            await log_to_ops_channel(bot, f"Removed the Room Judge role from {judges_removed} members")

        if not close_out:
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
                "closed_out_event": closed_out_event_id,
            },
        )

        # After a close-out the links were deleted with the event's live data, so there is no count to report
        links_line = "" if close_out else f"• Deactivated {deactivated} team links\n"
        await log_to_ops_channel(
            bot,
            f"Competition Cleanup Complete\n"
            f"{links_line}"
            f"• Deleted {deleted_count} team categories\n"
            f"• Removed {stats['roles_removed']} roles",
        )

        await update_status_channel(bot)

    except Exception as e:
        logger.exception(f"Cleanup error: {e}")
        await log_to_ops_channel(bot, f"Cleanup Error: {e}")


@dataclass
class BroadcastResult:
    sent: int = 0
    queued: int = 0
    failed: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    def ops_summary(self, sender: str, target: str, message: str) -> str:
        lines = [f"Broadcast by {sender}", f"• Target: {target}", f"• Sent: {self.sent} channels"]
        if self.queued:
            lines.append(f"• Queued: {self.queued} (pending channel creation)")
        if self.failed:
            lines.append(f"• Failed: {', '.join(self.failed)}")
        if self.missing:
            lines.append(f"• Not found: {', '.join(self.missing)}")
        lines.append(f"Message: {shorten(message, 100)}")
        return "\n".join(lines)


type Recipient = tuple[str, Callable[[], Awaitable[None]]]


async def deliver_broadcast(
    guild: discord.Guild, target: BroadcastTarget, message: str, sender: str
) -> BroadcastResult:
    """Send a broadcast to each recipient: the announcements channel, or each target team's chat channel (queued for
    a team without one). A recipient it fails for is listed in the result and the rest go on."""
    result = BroadcastResult()
    for label, deliver in await _recipients(guild, target, message, sender, result):
        try:
            await deliver()
        except Exception as e:
            logger.exception(f"Failed to broadcast to {label}: {e}")
            result.failed.append(f"{label} ({shorten(str(e), 50)})")
    return result


async def _recipients(
    guild: discord.Guild, target: BroadcastTarget, message: str, sender: str, result: BroadcastResult
) -> list[Recipient]:
    async def to_announcements() -> None:
        channel = guild.get_channel(settings.DISCORD_ANNOUNCEMENT_CHANNEL_ID)
        if not isinstance(channel, discord.TextChannel):
            raise RuntimeError(f"Announcements channel not found (ID: {settings.DISCORD_ANNOUNCEMENT_CHANNEL_ID})")
        await send_announcement(guild, channel, message)
        result.sent += 1

    def to_team(team: Team) -> Callable[[], Awaitable[None]]:
        async def deliver() -> None:
            chat_channel = team_chat_channel(guild, team)
            if chat_channel:
                await send_parts(chat_channel, announcement(sender, message))
                result.sent += 1
            else:
                await QueuedAnnouncement.objects.acreate(team=team, message=message, sender_name=sender)
                result.queued += 1

        return deliver

    match target:
        case Announcements():
            return [("Announcements", to_announcements)]
        case AllTeams():
            teams = [t async for t in Team.objects.filter(is_active=True).order_by("team_number")]
        case Teams(numbers):
            by_number = {t.team_number: t async for t in Team.objects.filter(team_number__in=numbers)}
            result.missing = [f"Team {n:02d}" for n in numbers if n not in by_number]
            teams = [by_number[n] for n in numbers if n in by_number]
    return [(f"Team {team.team_number:02d}", to_team(team)) for team in teams]
