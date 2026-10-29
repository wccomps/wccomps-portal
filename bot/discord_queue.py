"""Run the Discord tasks the portal queues, retrying failures with backoff."""

import asyncio
import logging
from collections.abc import Mapping
from datetime import timedelta
from typing import assert_never

import discord
from asgiref.sync import sync_to_async
from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Model, Q, QuerySet
from django.utils import timezone

from bot.competition_actions import deliver_broadcast, run_competition_cleanup
from bot.discord_manager import recreate_team_infrastructure, setup_team_infrastructure
from bot.heartbeat import record as record_heartbeat
from bot.role_sync import competition_guild, sync_member_roles, sync_roles
from bot.thread_creator import add_to_team_threads, ensure_ticket_thread
from bot.ticket_dashboard import trigger_dashboard
from bot.utils import (
    add_field,
    log_to_ops_channel,
    recycle_db_connection,
    send_parts,
)
from core.broadcast import parse_broadcast_target
from core.discord_tasks import (
    AddUserToThread,
    BroadcastMessage,
    CleanupCompetition,
    LogToChannel,
    PermanentTaskError,
    PostComment,
    PostTicketUpdate,
    SetupTeamInfrastructure,
    SyncMemberRoles,
    SyncRoles,
    SyncRolesResult,
    TaskPayload,
    TicketCreatedWeb,
)
from core.models import DiscordTask
from core.utils import role_sync_summary
from team.models import DiscordLink, Team
from ticketing.models import Ticket, TicketComment

logger = logging.getLogger(__name__)


async def _target[M: Model](queryset: QuerySet[M], **lookup: object) -> M:
    """The record a task is about; its absence fails the task at once."""
    try:
        return await queryset.aget(**lookup)
    except ObjectDoesNotExist:
        raise PermanentTaskError(f"{queryset.model.__name__} {lookup} does not exist") from None


class DiscordQueueProcessor:
    QUEUE_POLL_INTERVAL_SECONDS = 2
    QUEUE_BATCH_SIZE = 10
    MAX_BACKOFF_SECONDS = 300
    # A task left in "processing" by a dead bot is retried if younger than this, failed if older.
    STRANDED_TASK_MAX_AGE = timedelta(hours=1)

    def __init__(self, bot: discord.Client) -> None:
        self.bot = bot
        self.running = False
        self.task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self.running = True
        self.task = asyncio.create_task(self._process_loop())
        logger.info("Discord queue processor started")

    def _guild(self) -> discord.Guild:
        """The competition guild, looked up per task so a gateway reconnect can't leave a stale one."""
        guild = competition_guild(self.bot)
        if not guild:
            raise RuntimeError("Competition guild not found")
        return guild

    def stop(self) -> None:
        self.running = False
        if self.task:
            self.task.cancel()
        logger.info("Discord queue processor stopped")

    async def _process_loop(self) -> None:
        try:
            await self._recover_stranded_tasks()
        except Exception:
            logger.exception("Failed to recover stranded queue tasks")

        while self.running:
            try:
                await recycle_db_connection()
                await self._process_pending_tasks()
                record_heartbeat("queue", self.bot)
            except Exception as e:
                logger.exception(f"Error in queue processor: {e}")

            await asyncio.sleep(self.QUEUE_POLL_INTERVAL_SECONDS)

    @staticmethod
    @sync_to_async
    def _recover_stranded_tasks() -> None:
        """Requeue tasks a previous bot process left in "processing" when it died.

        Runs before the first poll. The bot Deployment is one replica with the Recreate
        strategy, so no other processor can be working on these rows.
        """
        cutoff = timezone.now() - DiscordQueueProcessor.STRANDED_TASK_MAX_AGE
        stranded = DiscordTask.objects.filter(status="processing")
        requeued = stranded.filter(created_at__gte=cutoff).update(status="pending", next_retry_at=None)
        failed = stranded.filter(created_at__lt=cutoff).update(
            status="failed", error_message="Left in processing by a bot restart; too old to retry"
        )
        if requeued or failed:
            logger.warning(f"Recovered stranded queue tasks: {requeued} requeued, {failed} failed as too old")

    async def _process_pending_tasks(self) -> None:

        @sync_to_async
        def get_pending_tasks() -> list[DiscordTask]:
            now = timezone.now()
            return list(
                DiscordTask.objects.filter(status="pending")
                .filter(Q(next_retry_at__isnull=True) | Q(next_retry_at__lte=now))
                .order_by("created_at")[: DiscordQueueProcessor.QUEUE_BATCH_SIZE]
            )

        tasks = await get_pending_tasks()

        for task in tasks:
            await self._process_task(task)

    async def _process_task(self, task: DiscordTask) -> None:

        # Claim with a conditional UPDATE so a task is never handled twice.
        claimed = await DiscordTask.objects.filter(pk=task.pk, status="pending").aupdate(status="processing")
        if not claimed:
            return
        task.status = "processing"

        try:
            task_result = await self._dispatch(task.typed_payload())
        except PermanentTaskError as error:
            if await self._record(task, status="failed", error_message=str(error)):
                logger.error(f"Task {task.id} ({task.task_type}) can never succeed: {error}")
                await log_to_ops_channel(
                    self.bot, f"Task {task.id} ({task.task_type}) failed and was not retried: {error}"
                )
        except Exception as error:
            retry_count = task.retry_count + 1
            if retry_count >= task.max_retries:
                if await self._record(task, status="failed", retry_count=retry_count, error_message=str(error)):
                    logger.exception(f"Task {task.id} failed after {task.max_retries} retries: {error}")
                    await log_to_ops_channel(
                        self.bot, f"Task {task.id} ({task.task_type}) failed after {task.max_retries} retries: {error}"
                    )
                return
            backoff_seconds = min(2**retry_count, DiscordQueueProcessor.MAX_BACKOFF_SECONDS)
            if await self._record(
                task,
                status="pending",
                retry_count=retry_count,
                error_message=str(error),
                next_retry_at=timezone.now() + timedelta(seconds=backoff_seconds),
            ):
                logger.warning(f"Task {task.id} failed (attempt {retry_count}), retrying in {backoff_seconds}s")
        else:
            result = dict(task_result) if task_result is not None else None
            if await self._record(task, status="completed", completed_at=timezone.now(), result=result):
                logger.info(f"Completed task {task.id}: {task.task_type}")

    @staticmethod
    async def _record(task: DiscordTask, **outcome: object) -> bool:
        """Write the task's outcome; False when its row is gone (deleting a ticket deletes its tasks)."""
        if not await DiscordTask.objects.filter(pk=task.pk).aupdate(**outcome):
            logger.info(f"Task {task.id} ({task.task_type}) was deleted while it ran")
            return False
        for name, value in outcome.items():
            setattr(task, name, value)
        return True

    async def _dispatch(self, payload: TaskPayload) -> Mapping[str, object] | None:
        """Run the handler for payload; what it returns is stored as the task's result."""
        match payload:
            case SyncMemberRoles():
                await self._handle_sync_member_roles(payload)
            case SetupTeamInfrastructure():
                await self._handle_setup_team_infrastructure(payload)
            case LogToChannel():
                await self._handle_log_to_channel(payload)
            case TicketCreatedWeb():
                await self._handle_ticket_created_web(payload)
            case CleanupCompetition():
                await self._handle_cleanup_competition(payload)
            case PostComment():
                await self._handle_post_comment(payload)
            case PostTicketUpdate():
                await self._handle_post_ticket_update(payload)
            case AddUserToThread():
                await self._handle_add_user_to_thread(payload)
            case SyncRoles():
                return await self._handle_sync_roles(payload)
            case BroadcastMessage():
                return await self._handle_broadcast_message(payload)
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

    async def _handle_cleanup_competition(self, payload: CleanupCompetition) -> None:
        """Run the competition cleanup Close Out Event queued: only the Discord side for a close-out."""
        await run_competition_cleanup(
            self.bot, self._guild(), payload.requested_by, closed_out_event_id=payload.closed_out_event_id
        )

    async def _handle_setup_team_infrastructure(self, payload: SetupTeamInfrastructure) -> None:
        team = await _target(Team.objects.all(), team_number=payload.team_number)
        if payload.recreate:
            reason = f"Team {team.team_number} infrastructure recreate"
            await recreate_team_infrastructure(self._guild(), team, reason, bot=self.bot)
        else:
            await setup_team_infrastructure(self._guild(), team, bot=self.bot)

    async def _handle_log_to_channel(self, payload: LogToChannel) -> None:
        if not payload.message:
            raise PermanentTaskError("Missing message in payload")

        await log_to_ops_channel(self.bot, payload.message)

    async def _handle_ticket_created_web(self, payload: TicketCreatedWeb) -> None:
        ticket = await _target(Ticket.objects.select_related("team"), id=payload.ticket_id)
        try:
            await ensure_ticket_thread(self.bot, self._guild(), ticket)
        finally:
            trigger_dashboard(self.bot)

    async def _handle_post_comment(self, payload: PostComment) -> None:
        ticket = await _target(Ticket.objects.select_related("team"), id=payload.ticket_id)
        comment = await _target(TicketComment.objects.select_related("author"), id=payload.comment_id)

        thread = await ensure_ticket_thread(self.bot, self._guild(), ticket)
        if thread is None:
            logger.info(f"Ticket {ticket.ticket_number} has no Discord thread; comment {comment.id} not mirrored")
            return

        author_display = (comment.author.username if comment.author else "") or "Unknown"
        # A web comment has no length limit; send all of it, in as many messages as it takes
        await send_parts(thread, f"**{author_display}**\n{comment.comment_text}")
        logger.info(f"Posted comment {comment.id} to thread {thread.id}")

    async def _handle_post_ticket_update(self, payload: PostTicketUpdate) -> None:
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
            await send_parts(thread, msg)
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

    async def _handle_broadcast_message(self, payload: BroadcastMessage) -> dict[str, object]:
        if not payload.target or not payload.message:
            raise PermanentTaskError("Missing target or message in payload")
        try:
            target = parse_broadcast_target(payload.target)
        except ValueError as e:
            raise PermanentTaskError(f"Invalid team range: {e}") from e

        result = await deliver_broadcast(self._guild(), target, payload.message, payload.sender)
        await log_to_ops_channel(self.bot, result.ops_summary(payload.sender, payload.target, payload.message))
        if result.failed and not (result.sent or result.queued):
            raise RuntimeError(f"Broadcast reached no one: {'; '.join(result.failed)}")
        return {"sent_count": result.sent, "queued_count": result.queued, "failed_count": len(result.failed)}
