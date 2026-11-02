"""Shared competition actions for commands and timer."""

import logging
from collections.abc import Awaitable, Callable
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import Literal

import discord
from asgiref.sync import sync_to_async
from django.conf import settings
from registration.models import Event
from registration.services import teardown_problem
from registration.stage import event_stage

from bot.discord_manager import delete_team_infrastructure, setup_team_infrastructure
from bot.heartbeat import beat
from bot.utils import (
    BlockingSteps,
    ProgressState,
    TaskProgress,
    announcement,
    log_to_ops_channel,
    send_announcement,
    send_parts,
    team_chat_channel,
)
from core.broadcast import AllTeams, Announcements, BroadcastTarget, Teams
from core.discord_tasks import PermanentTaskError
from core.models import AuditLog, CompetitionConfig, QueuedAnnouncement
from core.services.competition import START_MESSAGE_SENDER, Attempt, CompetitionRunResult
from core.services.competition import run_competition as competition_steps
from core.utils import shorten
from team.models import MAX_TEAMS, Team

logger = logging.getLogger(__name__)


async def run_competition(enable: bool, actor: str, timer: Attempt | None = None) -> CompetitionRunResult:
    """Start (enable=True) or stop the competition, a step at a time on a pool thread of its own, beating after each."""
    async with aclosing(BlockingSteps(competition_steps(enable, actor, timer))) as steps:
        async for _step in steps:
            beat()
        return steps.result


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
    done.member_roles_removed = (await sync_roles(guild))["roles_removed"]
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
    bot: discord.Client, guild: discord.Guild, actor: str, open_event_id: int | None, beat: Callable[[], None] = beat
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
        async for team in Team.objects.filter(members__is_active=True).distinct().order_by("team_number"):
            await setup_team_infrastructure(guild, team, bot=bot)
            beat()
        await sync_roles(guild)
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


@dataclass
class BroadcastResult:
    sent: int = 0
    queued: int = 0
    failed: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    # Some failure may succeed on a retry (not Forbidden or a PermanentTaskError)
    retryable: bool = False

    def ops_summary(self, sender: str, target: str, message: str) -> str:
        lines = [f"Broadcast by {sender}", f"• Target: {target}", f"• Sent: {self.sent} channels"]
        if self.queued:
            lines.append(f"• Queued: {self.queued} (pending channel creation)")
        if self.failed:
            lines.append(f"• Failed: {', '.join(self.failed)}")
        if self.missing:
            lines.append(f"• Not found: {', '.join(self.missing)}")
        if sender != START_MESSAGE_SENDER:
            lines.append(f"Message: {shorten(message, 100)}")
        return "\n".join(lines)


class _BroadcastStoppedError(Exception):
    pass


type Delivery = Literal["sent", "queued"]
# (the recipient's key: 0, or a team's number; its label; its delivery, given its sent parts' progress)
type Recipient = tuple[int, str, Callable[[TaskProgress], Awaitable[Delivery]]]


async def deliver_broadcast(
    guild: discord.Guild,
    target: BroadcastTarget,
    message: str,
    sender: str,
    progress: TaskProgress | None = None,
    *,
    wanted: Callable[[], Awaitable[bool]] | None = None,
    queue_copy: Callable[[Team, str, str], Awaitable[bool]] | None = None,
) -> BroadcastResult:
    """Send a broadcast to each recipient, listing those it fails for; progress lets a retry try only the rest.
    wanted, asked before each team, stops it: the teams not reached are listed as failed. queue_copy, when given,
    queues a team's copy and returns False instead when the broadcast was stopped."""
    progress = progress or TaskProgress()
    state = progress.state
    reached, refused, parts = (
        state.setdefault("reached", []),
        state.setdefault("refused", {}),
        state.setdefault("parts", {}),
    )

    def parts_sent(key: str) -> TaskProgress:
        async def save(part: ProgressState) -> None:
            parts[key] = part.get("done", 0)
            await progress.saved()

        return TaskProgress(ProgressState(done=parts.get(key, 0)), save)

    result = BroadcastResult()
    recipients = await _recipients(guild, target, message, sender, result, queue_copy)
    for index, (key, label, deliver) in enumerate(recipients):
        if key in reached or str(key) in refused:
            continue
        if key != 0 and wanted is not None and not await wanted():
            result.failed += [
                f"{rest_label} (not sent: the broadcast was stopped)"
                for rest_key, rest_label, _ in recipients[index:]
                if rest_key not in reached and str(rest_key) not in refused
            ]
            break
        try:
            delivery = await deliver(parts_sent(str(key)))
        except _BroadcastStoppedError:
            result.failed += [
                f"{rest_label} (not sent: the broadcast was stopped)"
                for rest_key, rest_label, _ in recipients[index:]
                if rest_key not in reached and str(rest_key) not in refused
            ]
            break
        except Exception as e:
            logger.exception(f"Failed to broadcast to {label}: {e}")
            reason = f"{label} ({shorten(str(e), 50)})"
            # NotFound is retried: a recreate may be replacing the team's channel
            if isinstance(e, discord.Forbidden | PermanentTaskError):
                refused[str(key)] = reason
                await progress.saved()
            else:
                result.failed.append(reason)
                result.retryable = True
        else:
            parts.pop(str(key), None)
            reached.append(key)
            state[delivery] = state.get(delivery, 0) + 1
            await progress.saved()
        beat()
    result.sent, result.queued = state.get("sent", 0), state.get("queued", 0)
    result.failed += refused.values()
    return result


async def _recipients(
    guild: discord.Guild,
    target: BroadcastTarget,
    message: str,
    sender: str,
    result: BroadcastResult,
    queue_copy: Callable[[Team, str, str], Awaitable[bool]] | None = None,
) -> list[Recipient]:
    async def to_announcements(parts: TaskProgress) -> Delivery:
        channel = guild.get_channel(settings.DISCORD_ANNOUNCEMENT_CHANNEL_ID)
        if not isinstance(channel, discord.TextChannel):
            raise PermanentTaskError(
                f"Announcements channel not found (ID: {settings.DISCORD_ANNOUNCEMENT_CHANNEL_ID})"
            )
        await send_announcement(guild, channel, message, progress=parts)
        return "sent"

    def to_team(team: Team) -> Callable[[TaskProgress], Awaitable[Delivery]]:
        async def deliver(parts: TaskProgress) -> Delivery:
            chat_channel = team_chat_channel(guild, team)
            if chat_channel:
                await send_parts(chat_channel, announcement(sender, message), progress=parts)
                return "sent"
            if queue_copy is not None:
                if not await queue_copy(team, message, sender):
                    raise _BroadcastStoppedError
            else:
                await QueuedAnnouncement.objects.acreate(team=team, message=message, sender_name=sender)
            return "queued"

        return deliver

    match target:
        case Announcements():
            return [(0, "Announcements", to_announcements)]
        case AllTeams():
            teams = [t async for t in Team.objects.filter(is_active=True).order_by("team_number")]
        case Teams(numbers):
            by_number = {t.team_number: t async for t in Team.objects.filter(team_number__in=numbers)}
            result.missing = [f"Team {n:02d}" for n in numbers if n not in by_number]
            teams = [by_number[n] for n in numbers if n in by_number]
    teams.sort(key=lambda team: team.team_number)
    return [(team.team_number, f"Team {team.team_number:02d}", to_team(team)) for team in teams]
