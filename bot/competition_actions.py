"""Shared competition actions for commands and timer."""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import discord
from asgiref.sync import sync_to_async
from django.conf import settings
from registration.models import Event
from registration.services import teardown_problem
from registration.stage import event_stage

from bot.discord_manager import DiscordManager, delete_team_infrastructure
from bot.utils import announcement, log_to_ops_channel, run_blocking, send_announcement, send_parts, team_chat_channel
from core.authentik_utils import parse_team_range
from core.models import AuditLog, CompetitionConfig, QueuedAnnouncement
from core.services.competition import CompetitionRunResult, run_competition_to_completion
from core.utils import shorten
from team.models import MAX_TEAMS, Team

logger = logging.getLogger(__name__)


async def run_competition(enable: bool, actor: str, on_step: Callable[[], None] | None = None) -> CompetitionRunResult:
    """Start (enable=True) or stop the competition via the shared service, off the shared thread."""
    return await run_blocking(run_competition_to_completion, enable, actor, on_step)


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


@dataclass
class Teardown:
    """What tearing the team channels and roles down did, kept up to date as it goes; stopped says why it stopped
    early, or ''."""

    member_roles_removed: int = 0
    categories: int = 0
    team_roles: int = 0
    judges: int = 0
    left: list[int] = field(default_factory=list)
    stopped: str = ""

    @property
    def changed(self) -> bool:
        return bool(self.member_roles_removed or self.categories or self.team_roles or self.judges)


async def _tear_down_teams(
    guild: discord.Guild, problem: Callable[[], str], beat: Callable[[], None], done: Teardown
) -> None:
    """Delete every team's category, channels and role, then take the Room Judge role away; problem, asked before each
    team, stops it early. Counts go into done as they happen."""
    from bot.role_sync import sync_roles

    # While the role IDs are recorded, so the sync still counts team roles as its own
    done.member_roles_removed = (await sync_roles(guild, beat=beat))["roles_removed"]
    async for team in Team.objects.all().order_by("team_number"):
        if why := await sync_to_async(problem)():
            done.stopped = why
            return
        removed = await delete_team_infrastructure(guild, team, reason="Competition cleanup")
        done.categories += removed.count("category")
        done.team_roles += removed.count("role")
        if team.channels_need_clearing:
            done.left.append(team.team_number)
        beat()
    done.judges = await _remove_room_judges(guild)


async def _remove_room_judges(guild: discord.Guild) -> int:
    room_judge_role = discord.utils.get(guild.roles, name="WCComps Room Judge")
    if room_judge_role is None:
        return 0
    judges = 0
    for member in room_judge_role.members:
        try:
            await member.remove_roles(room_judge_role, reason="Competition cleanup")
            judges += 1
        except Exception as e:
            logger.warning(f"Could not remove Room Judge from {member}: {e}")
    return judges


async def reconcile_teams(
    bot: discord.Client, guild: discord.Guild, actor: str, open_event_id: int | None, beat: Callable[[], None]
) -> None:
    """Tear the team channels and roles down unless teardown_problem refuses, then set the linked teams up again,
    even after a teardown that failed; a failure raises once both have run."""

    def problem() -> str:
        return teardown_problem(event_stage(), open_event_id)

    skipped = await sync_to_async(problem)()
    if skipped:
        await log_to_ops_channel(bot, f"Discord cleanup skipped: {skipped}")
    event = await Event.objects.select_related("season").filter(pk=open_event_id).afirst() if open_event_id else None
    done = Teardown()
    failure: Exception | None = None
    if not skipped:
        try:
            await _tear_down_teams(guild, problem, beat, done)
        except Exception as e:
            failure = e

    from bot.role_sync import sync_roles

    try:
        manager = DiscordManager(guild, bot)
        async for team in Team.objects.filter(members__is_active=True).distinct().order_by("team_number"):
            await manager.setup_team_infrastructure(team.team_number)
            beat()
        await sync_roles(guild, beat=beat)
    except Exception as e:
        failure = failure or e
    quiet = event is not None and failure is None and not (done.changed or done.stopped or done.left)
    if not skipped and not quiet:
        await AuditLog.objects.acreate(
            action="competition_cleanup",
            admin_user=actor,
            target_entity="competition",
            target_id=open_event_id or 0,
            details={
                "event": open_event_id,
                "deleted_categories": done.categories,
                "deleted_team_roles": done.team_roles,
                "member_roles_removed": done.member_roles_removed,
                "room_judges_removed": done.judges,
                "stopped": done.stopped,
                "left": done.left,
                "finished": failure is None,
            },
        )
        await _report_teardown(bot, event, done, failure)
    if failure:
        raise failure


async def _report_teardown(bot: discord.Client, event: Event | None, done: Teardown, failure: Exception | None) -> None:
    title = f"{event} opened: earlier events' team channels removed" if event else "Discord Cleanup Complete"
    if failure is not None:
        title = f"Discord cleanup failed partway: {shorten(str(failure), 200)}"
    lines = [
        title,
        f"• Deleted {done.categories} team categories and {done.team_roles} team roles",
        f"• Removed {done.member_roles_removed} roles from members who are no longer linked",
    ]
    if done.judges:
        lines.append(f"• Removed the Room Judge role from {done.judges} members")
    if done.left:
        teams = ", ".join(f"{number:02d}" for number in done.left)
        lines.append(
            f"• Could not delete the role or category of teams {teams}: delete them in Discord, then run "
            "/teams recreate for those teams"
        )
    if done.stopped:
        lines.append(f"Stopped before the end: {done.stopped}")
    await log_to_ops_channel(bot, "\n".join(lines))
    await update_status_channel(bot)


async def deliver_team_broadcast(
    guild: discord.Guild,
    target: str,
    message: str,
    sender: str,
    wanted: Callable[[], Awaitable[bool]] | None = None,
    queue_copy: Callable[[Team, str, str], Awaitable[bool]] | None = None,
) -> tuple[int, int, list[str]]:
    """Send a broadcast to 'announcements', 'all-teams', or a team range, queuing if a channel is missing. wanted,
    asked before each team, stops it: the teams not reached are listed as failed. queue_copy, when given, queues a
    team's copy and returns False instead when the broadcast was stopped."""
    target_lower = target.lower().strip()
    if target_lower == "announcements":
        announcement_channel_id = settings.DISCORD_ANNOUNCEMENT_CHANNEL_ID
        channel = guild.get_channel(announcement_channel_id)
        if not channel or not isinstance(channel, discord.TextChannel):
            return 0, 0, [f"Announcements channel not found (ID: {announcement_channel_id})"]
        try:
            await send_announcement(guild, channel, message)
            return 1, 0, []
        except Exception as e:
            logger.exception(f"Failed to broadcast to announcements: {e}")
            return 0, 0, [f"Announcements ({shorten(str(e), 50)})"]

    if target_lower == "all-teams":
        teams = [t async for t in Team.objects.filter(is_active=True).order_by("team_number")]
        team_numbers = [t.team_number for t in teams]
        teams_by_number = {t.team_number: t for t in teams}
    else:
        team_numbers = parse_team_range(target)
        teams_by_number = {t.team_number: t async for t in Team.objects.filter(team_number__in=team_numbers)}

    sent_count = 0
    queued_count = 0
    failed_channels: list[str] = []

    for i, team_number in enumerate(team_numbers):
        if wanted is not None and not await wanted():
            failed_channels += [
                f"Team {number:02d} (not sent: the broadcast was stopped)" for number in team_numbers[i:]
            ]
            break
        team = teams_by_number.get(team_number)
        if not team:
            failed_channels.append(f"Team {team_number:02d} (not found)")
            continue
        try:
            chat_channel = team_chat_channel(guild, team)
            if chat_channel:
                await send_parts(chat_channel, announcement(sender, message))
                sent_count += 1
            elif queue_copy is not None and not await queue_copy(team, message, sender):
                failed_channels += [
                    f"Team {number:02d} (not sent: the broadcast was stopped)" for number in team_numbers[i:]
                ]
                break
            else:
                if queue_copy is None:
                    await QueuedAnnouncement.objects.acreate(team=team, message=message, sender_name=sender)
                queued_count += 1
        except Exception as e:
            logger.exception(f"Failed to broadcast to team {team_number}: {e}")
            failed_channels.append(f"Team {team_number:02d} ({shorten(str(e), 50)})")

    return sent_count, queued_count, failed_channels
