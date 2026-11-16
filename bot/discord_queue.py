"""Run the Discord tasks the portal queues; core.task_queue polls, claims and retries them."""

import asyncio
import logging
from collections.abc import Mapping
from typing import assert_never

import discord

from bot.competition_actions import BroadcastState, deliver_broadcast, update_status_channel
from bot.discord_manager import recreate_team_infrastructure, setup_team_infrastructure
from bot.role_sync import competition_guild, sync_member_roles, sync_roles
from bot.thread_creator import add_to_team_threads, ensure_ticket_thread
from bot.ticket_dashboard import trigger_dashboard
from bot.utils import add_field, log_to_ops_channel, send_parts
from core.broadcast import parse_broadcast_target
from core.heartbeat import QUEUE_DISCORD, beat, beat_for
from core.models import QueuedTask
from core.queued_tasks import (
    AddUserToThread,
    BroadcastMessage,
    ChatNotice,
    ClearTeamChannels,
    CompetitionStatusChanged,
    DiscordOnly,
    LogToChannel,
    PermanentTaskError,
    PostComment,
    PostTicketUpdate,
    ReconcileTeams,
    SetupTeamInfrastructure,
    SyncMemberRoles,
    SyncRoles,
    SyncRolesResult,
    TicketCreated,
)
from core.task_backends import TaskBackend
from core.task_queue import QueueProcessor, StepsState, TaskProgress, _target
from core.utils import role_sync_summary
from team.models import DiscordLink, Team
from ticketing.models import Ticket, TicketComment

logger = logging.getLogger(__name__)


class DiscordQueueProcessor(QueueProcessor[ChatNotice | DiscordOnly]):
    backend = TaskBackend.DISCORD
    heartbeat_name = QUEUE_DISCORD

    def __init__(self, bot: discord.Client) -> None:
        # on_start binds the loop's task to the heartbeat, so beat() in code a handler awaits (reconcile_teams,
        # sync_roles) advances it too
        super().__init__(on_start=lambda name: beat_for(name, lambda: not bot.is_closed()), on_pass=beat)
        self.bot = bot

    def _guild(self) -> discord.Guild:
        """The competition guild, looked up per task so a gateway reconnect can't leave a stale one."""
        guild = competition_guild(self.bot)
        if not guild:
            raise RuntimeError("Competition guild not found")
        return guild

    async def report(self, message: str) -> None:
        await log_to_ops_channel(self.bot, message)

    def retry_after(self, error: Exception) -> float | None:
        return error.retry_after if isinstance(error, discord.RateLimited) else None

    async def dispatch(self, payload: ChatNotice | DiscordOnly, task: QueuedTask) -> Mapping[str, object] | None:
        match payload:
            case SyncMemberRoles():
                await self._handle_sync_member_roles(payload)
            case SetupTeamInfrastructure():
                await self._handle_setup_team_infrastructure(payload, TaskProgress.of(task))
            case LogToChannel():
                await self._handle_log_to_channel(payload)
            case TicketCreated():
                await self._handle_ticket_created_web(payload)
            case ReconcileTeams():
                await self._handle_reconcile_teams(payload)
            case ClearTeamChannels():
                await self._handle_clear_team_channels(payload)
            case PostComment():
                await self._handle_post_comment(payload, TaskProgress.of(task))
            case PostTicketUpdate():
                await self._handle_post_ticket_update(payload, TaskProgress.of(task))
            case AddUserToThread():
                await self._handle_add_user_to_thread(payload)
            case SyncRoles():
                return await self._handle_sync_roles(payload)
            case BroadcastMessage():
                return await self._handle_broadcast_message(payload, TaskProgress.of(task))
            case CompetitionStatusChanged():
                await update_status_channel(self.bot)
            case _:
                assert_never(payload)
        return None

    async def _handle_sync_member_roles(self, payload: SyncMemberRoles) -> None:
        guild = self._guild()
        member = guild.get_member(payload.discord_id)
        if not member:
            try:
                member = await guild.fetch_member(payload.discord_id)
            except discord.NotFound:
                logger.info(f"Member {payload.discord_id} not in the guild; joining queues their sync")
                return

        # Problems are logged, not raised: retrying can't fix a missing permission or role, and the
        # periodic sync retries this member anyway
        stats = await sync_member_roles(guild, member)
        logger.info(
            f"Synced roles for {member}: {stats['roles_added']} added, {stats['roles_removed']} removed"
            + (f"; problems: {'; '.join(c for c in stats['changes'] if c.startswith('⚠'))}" if stats["errors"] else "")
        )
        link = await DiscordLink.objects.filter(discord_id=payload.discord_id, is_active=True).afirst()
        if link and link.team_id:
            await add_to_team_threads(self.bot, member, link.team_id)

    async def _handle_reconcile_teams(self, payload: ReconcileTeams) -> None:
        from bot.competition_actions import reconcile_teams

        await reconcile_teams(self.bot, self._guild(), payload.requested_by, payload.open_event_id, beat)

    async def _handle_clear_team_channels(self, payload: ClearTeamChannels) -> None:
        from bot.discord_manager import delete_team_infrastructure

        number = payload.team_number
        team = await Team.objects.filter(team_number=number).afirst()
        if team is None or not team.channels_need_clearing:
            return
        await delete_team_infrastructure(
            self._guild(), team, reason=f"Team {number} handed to a new school", then_setup=True
        )
        if await Team.objects.filter(pk=team.pk, channels_need_clearing=True).aexists():
            raise RuntimeError(
                f"Team {number}'s old Discord role or category could not be deleted. Delete them in Discord, then run "
                f"/teams recreate for team {number}."
            )

    async def _handle_setup_team_infrastructure(
        self, payload: SetupTeamInfrastructure, progress: TaskProgress[StepsState]
    ) -> None:
        team = await _target(Team.objects.all(), team_number=payload.team_number)
        if payload.recreate:
            reason = f"Team {team.team_number} infrastructure recreate"
            await recreate_team_infrastructure(self._guild(), team, reason, bot=self.bot, progress=progress)
        else:
            await setup_team_infrastructure(self._guild(), team, bot=self.bot)

    async def _handle_log_to_channel(self, payload: LogToChannel) -> None:
        if not payload.message:
            raise PermanentTaskError("Missing message in payload")

        await log_to_ops_channel(self.bot, payload.message)

    async def _handle_ticket_created_web(self, payload: TicketCreated) -> None:
        ticket = await _target(Ticket.objects.select_related("team"), id=payload.ticket_id)
        try:
            await ensure_ticket_thread(self.bot, self._guild(), ticket)
        finally:
            trigger_dashboard(self.bot)

    async def _handle_post_comment(self, payload: PostComment, progress: TaskProgress[StepsState]) -> None:
        ticket = await _target(Ticket.objects.select_related("team"), id=payload.ticket_id)
        comment = await _target(TicketComment.objects.select_related("author"), id=payload.comment_id)

        thread = await ensure_ticket_thread(self.bot, self._guild(), ticket)
        if thread is None:
            logger.info(f"Ticket {ticket.ticket_number} has no Discord thread; comment {comment.id} not mirrored")
            return

        # A web comment has no length limit; send all of it, in as many messages as it takes
        await send_parts(thread, f"**{comment.author_name}**\n{comment.comment_text}", progress=progress)
        logger.info(f"Posted comment {comment.id} to thread {thread.id}")

    async def _handle_post_ticket_update(self, payload: PostTicketUpdate, progress: TaskProgress[StepsState]) -> None:
        action = payload.action
        actor = payload.actor

        ticket = await _target(Ticket.objects.select_related("team"), id=payload.ticket_id)
        trigger_dashboard(self.bot)

        thread = await ensure_ticket_thread(self.bot, self._guild(), ticket)
        if thread is None:
            logger.info(f"Ticket {ticket.ticket_number} has no Discord thread; update '{action}' not mirrored")
            return

        if action == "resolved":
            notes = payload.resolution_notes
            points = payload.points_charged
            embed = discord.Embed(
                title="Ticket Resolved",
                color=discord.Color.green(),
            )
            embed.add_field(name="Resolved By", value=actor, inline=True)
            embed.add_field(name="Points Charged", value=str(points), inline=True)
            add_field(embed, "Resolution Notes", notes, inline=False)
            await thread.send(embed=embed)
        elif action == "claimed":
            await thread.send(f"Ticket claimed by **{actor}**")
        elif action == "assigned":
            await thread.send(f"Ticket assigned to **{payload.assignee}** by **{actor}**")
        elif action == "unclaimed":
            await thread.send(f"Ticket unclaimed by **{actor}**")
        elif action == "cancelled":
            await thread.send(f"Ticket cancelled by **{actor}**")
        elif action == "reopened":
            # Resolving archived and locked the thread (cogs/ticketing.py); only staff can post in a locked one
            if thread.archived or thread.locked:
                await thread.edit(archived=False, locked=False)
            msg = f"Ticket reopened by **{actor}**"
            if payload.reason:
                msg += f"\nReason: {payload.reason}"
            await send_parts(thread, msg, progress=progress)
        elif action == "points_adjusted":
            msg = f"Ticket points adjusted to **{payload.points_charged}** by **{actor}**"
            if payload.reason:
                msg += f"\nReason: {payload.reason}"
            await send_parts(thread, msg, progress=progress)
        else:
            await thread.send(f"Ticket updated: {action} by **{actor}**")

        logger.info(f"Posted ticket update ({action}) to thread {thread.id}")

    async def _handle_add_user_to_thread(self, payload: AddUserToThread) -> None:
        discord_id = payload.discord_id
        ticket = await _target(Ticket.objects.select_related("team"), id=payload.ticket_id)

        thread = await ensure_ticket_thread(self.bot, self._guild(), ticket)
        if thread is None:
            logger.info(f"Ticket {ticket.ticket_number} has no Discord thread; user {discord_id} not added")
            return

        user = self.bot.get_user(discord_id)
        if not user:
            try:
                user = await self.bot.fetch_user(discord_id)
            except Exception as e:
                raise ValueError(f"Could not find user {discord_id}: {e}") from e

        await thread.add_user(user)

        logger.info(f"Added user {discord_id} to thread {thread.id}")

    async def _handle_sync_roles(self, payload: SyncRoles) -> SyncRolesResult:
        """Run the Authentik role sync; its result is what the web Sync Roles page shows."""
        dry_run = payload.dry_run
        guild = self._guild()

        try:
            stats = await asyncio.wait_for(sync_roles(guild, dry_run=dry_run), timeout=300.0)
        except TimeoutError:
            logger.error("Role sync timed out after 5 minutes")
            raise RuntimeError("Role sync timed out after 5 minutes") from None

        summary = role_sync_summary(stats, dry_run=dry_run)
        await log_to_ops_channel(self.bot, summary)

        logger.info(f"Role sync completed: {summary}")
        return stats

    async def _handle_broadcast_message(
        self, payload: BroadcastMessage, progress: TaskProgress[BroadcastState]
    ) -> dict[str, object]:
        if not payload.target or not payload.message:
            raise PermanentTaskError("Missing target or message in payload")
        try:
            target = parse_broadcast_target(payload.target)
        except ValueError as e:
            raise PermanentTaskError(f"Invalid team range: {e}") from e

        result = await deliver_broadcast(self._guild(), target, payload.message, payload.sender, progress)
        summary = result.ops_summary(payload.sender, payload.target, payload.message)
        if result.failed:
            # A retry tries only the recipients that may yet succeed; the last failure posts this summary
            raise (RuntimeError if result.retryable else PermanentTaskError)(summary)
        await log_to_ops_channel(self.bot, summary)
        return {"sent_count": result.sent, "queued_count": result.queued, "failed_count": len(result.failed)}
