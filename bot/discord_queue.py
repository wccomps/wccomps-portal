"""Discord task queue processor for rate limit resilience."""

import asyncio
import logging
from collections.abc import Mapping
from datetime import timedelta
from typing import assert_never

import discord
from asgiref.sync import sync_to_async
from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from bot.discord_manager import DiscordManager
from bot.heartbeat import record as record_heartbeat
from bot.role_sync import competition_guild
from bot.thread_creator import publish_new_ticket
from bot.ticket_dashboard import trigger_dashboard
from bot.utils import DISCORD_EMBED_FIELD_CHAR_LIMIT, recycle_db_connection, split_message, team_chat_channel
from core.discord_tasks import (
    PAYLOAD_TYPES,
    AddUserToThread,
    BroadcastMessage,
    CleanupCompetition,
    LogToChannel,
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
from team.models import Team
from ticketing.models import Ticket, TicketComment

logger = logging.getLogger(__name__)


class DiscordQueueProcessor:
    """Process Discord tasks from database queue using async tasks."""

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

    def _manager(self) -> DiscordManager:
        return DiscordManager(self._guild(), self.bot)

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
            if task.task_type not in PAYLOAD_TYPES:
                logger.warning(f"Unknown task type: {task.task_type}")
                task.status = "failed"
                task.error_message = f"Unknown task type: {task.task_type}"
                await task.asave()
                return
            task_result = await self._dispatch(task.typed_payload())

            @sync_to_async
            def mark_completed() -> None:
                task.status = "completed"
                task.completed_at = timezone.now()
                task.result = dict(task_result) if task_result is not None else None
                task.save()

            await mark_completed()
            logger.info(f"Completed task {task.id}: {task.task_type}")

        except discord.errors.RateLimited as rate_limit_error:

            @sync_to_async
            def handle_rate_limit(error: discord.errors.RateLimited) -> float:
                retry_after = error.retry_after
                task.retry_count += 1
                task.next_retry_at = timezone.now() + timedelta(seconds=retry_after)
                task.status = "pending"
                task.error_message = f"Rate limited, retry after {retry_after}s"
                task.save()
                return retry_after

            retry_after = await handle_rate_limit(rate_limit_error)
            logger.warning(f"Task {task.id} rate limited, retrying in {retry_after}s")

        except Exception as error:

            @sync_to_async
            def handle_error(exc: Exception) -> tuple[str, int]:
                task.retry_count += 1
                task.error_message = str(exc)

                if task.retry_count >= task.max_retries:
                    task.status = "failed"
                    task.save()
                    return "failed", task.max_retries
                backoff_seconds = min(2**task.retry_count, DiscordQueueProcessor.MAX_BACKOFF_SECONDS)
                task.next_retry_at = timezone.now() + timedelta(seconds=backoff_seconds)
                task.status = "pending"
                task.save()
                return "retry", backoff_seconds

            result, value = await handle_error(error)

            if result == "failed":
                logger.exception(f"Task {task.id} failed after {value} retries: {error}")
                try:
                    from bot.utils import log_to_ops_channel

                    await log_to_ops_channel(
                        self.bot,
                        f"Task {task.id} ({task.task_type}) failed after {value} retries: {error}",
                    )
                except Exception as log_error:
                    logger.exception(f"Failed to log error to ops channel: {log_error}")
            else:
                logger.warning(f"Task {task.id} failed (attempt {task.retry_count}), retrying in {value}s")

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
        from bot.role_sync import sync_member_roles

        guild = self._guild()
        member = guild.get_member(payload.discord_id)
        if not member:
            try:
                member = await guild.fetch_member(payload.discord_id)
            except discord.NotFound:
                logger.info(
                    f"Member {payload.discord_id} not in the guild; the periodic role sync covers them once they join"
                )
                return

        # Problems are logged, not raised: retrying can't fix a missing permission or role, and the
        # periodic sync retries this member anyway
        stats = await sync_member_roles(guild, member)
        logger.info(
            f"Synced roles for {member}: {stats['roles_added']} added, {stats['roles_removed']} removed"
            + (f"; problems: {'; '.join(c for c in stats['changes'] if c.startswith('⚠'))}" if stats["errors"] else "")
        )

    async def _handle_cleanup_competition(self, payload: CleanupCompetition) -> None:
        """Run the competition cleanup requested from the ops page."""
        from bot.competition_actions import run_competition_cleanup

        await run_competition_cleanup(self.bot, self._guild(), payload.requested_by)

    async def _handle_setup_team_infrastructure(self, payload: SetupTeamInfrastructure) -> None:
        team_number = payload.team_number

        role, category = await self._manager().setup_team_infrastructure(team_number)
        if not role or not category:
            raise RuntimeError(f"Failed to setup infrastructure for team {team_number}")

        logger.info(f"Set up infrastructure for team {team_number}")

    async def _handle_log_to_channel(self, payload: LogToChannel) -> None:
        if not payload.message:
            raise ValueError("Missing message in payload")

        from bot.utils import log_to_ops_channel

        await log_to_ops_channel(self.bot, payload.message)

    async def _handle_ticket_created_web(self, payload: TicketCreatedWeb) -> None:
        """Handle ticket creation from web UI - create thread and post to dashboard."""
        ticket = await Ticket.objects.select_related("team").aget(id=payload.ticket_id)

        # A retry after a partial failure must not create a second thread.
        if ticket.discord_thread_id:
            trigger_dashboard(self.bot)
            return

        await publish_new_ticket(self.bot, competition_guild(self.bot), ticket)

    async def _handle_post_comment(self, payload: PostComment) -> None:
        """Handle posting a comment from web to Discord thread."""
        ticket_id = payload.ticket_id
        comment_id = payload.comment_id

        @sync_to_async
        def get_data() -> tuple[Ticket, TicketComment]:
            ticket = Ticket.objects.get(id=ticket_id)
            comment = TicketComment.objects.select_related("author").get(id=comment_id)
            return ticket, comment

        ticket, comment = await get_data()

        if not ticket.discord_thread_id:
            logger.info(f"Ticket {ticket.ticket_number} has no Discord thread; comment {comment_id} not mirrored")
            return

        thread = self.bot.get_channel(ticket.discord_thread_id)
        if not thread:
            try:
                thread = await self.bot.fetch_channel(ticket.discord_thread_id)
            except Exception as e:
                raise ValueError(f"Could not find thread {ticket.discord_thread_id}: {e}") from e

        if not isinstance(thread, (discord.TextChannel, discord.Thread)):
            raise TypeError(f"Channel {ticket.discord_thread_id} is not a text channel or thread")

        author_display = "Unknown"
        if comment.author:
            author_display = comment.author.username or "Unknown"
        message_content = f"**{author_display}**\n{comment.comment_text}"

        # A web comment has no length limit; send all of it, in as many messages as it takes
        messages = [await thread.send(part) for part in split_message(message_content)]
        message = messages[0]

        @sync_to_async
        def save_message_id() -> None:
            comment.discord_message_id = message.id
            comment.save()

        await save_message_id()

        logger.info(f"Posted comment {comment_id} to thread {thread.id} (message {message.id})")

    async def _handle_post_ticket_update(self, payload: PostTicketUpdate) -> None:
        """Post a ticket status update (resolve/claim/unclaim/reopen) to the Discord thread."""
        action = payload.action
        actor = payload.actor

        ticket = await Ticket.objects.aget(id=payload.ticket_id)
        trigger_dashboard(self.bot)

        if not ticket.discord_thread_id:
            logger.info(f"Ticket {ticket.ticket_number} has no Discord thread; update '{action}' not mirrored")
            return

        thread = self.bot.get_channel(ticket.discord_thread_id)
        if not thread:
            try:
                thread = await self.bot.fetch_channel(ticket.discord_thread_id)
            except Exception as e:
                raise ValueError(f"Could not find thread {ticket.discord_thread_id}: {e}") from e

        if not isinstance(thread, (discord.TextChannel, discord.Thread)):
            raise TypeError(f"Channel {ticket.discord_thread_id} is not a text channel or thread")

        if action == "resolved":
            notes = payload.resolution_notes
            points = payload.points_charged
            embed = discord.Embed(
                title="Ticket Resolved",
                color=discord.Color.green(),
            )
            embed.add_field(name="Resolved By", value=actor, inline=True)
            embed.add_field(name="Points Charged", value=str(points), inline=True)
            if notes:
                embed.add_field(name="Resolution Notes", value=notes[:DISCORD_EMBED_FIELD_CHAR_LIMIT], inline=False)
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
            if isinstance(thread, discord.Thread) and (thread.archived or thread.locked):
                await thread.edit(archived=False, locked=False)
            msg = f"Ticket reopened by **{actor}**"
            if payload.reason:
                msg += f"\nReason: {payload.reason}"
            await thread.send(msg)
        else:
            await thread.send(f"Ticket updated: {action} by **{actor}**")

        logger.info(f"Posted ticket update ({action}) to thread {ticket.discord_thread_id}")

    async def _handle_add_user_to_thread(self, payload: AddUserToThread) -> None:
        discord_id = payload.discord_id
        thread_id = payload.thread_id

        thread = self.bot.get_channel(thread_id)
        if not thread:
            try:
                thread = await self.bot.fetch_channel(thread_id)
            except Exception as e:
                raise ValueError(f"Could not find thread {thread_id}: {e}") from e

        if not isinstance(thread, discord.Thread):
            raise TypeError(f"Channel {thread_id} is not a thread")

        user = self.bot.get_user(discord_id)
        if not user:
            try:
                user = await self.bot.fetch_user(discord_id)
            except Exception as e:
                raise ValueError(f"Could not find user {discord_id}: {e}") from e

        await thread.add_user(user)

        logger.info(f"Added user {discord_id} to thread {thread_id}")

    async def _handle_sync_roles(self, payload: SyncRoles) -> SyncRolesResult:
        """Run the Authentik role sync; its result is what the web Sync Roles page shows."""
        from bot.role_sync import sync_roles
        from bot.utils import log_to_ops_channel

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
        """Broadcast a message to announcement channel or team channels."""
        from bot.utils import log_to_ops_channel
        from core.authentik_utils import parse_team_range

        target = payload.target
        message = payload.message
        sender = payload.sender

        if not target or not message:
            raise ValueError("Missing target or message in payload")

        guild = self._guild()

        target_lower = target.lower().strip()
        sent_count = 0
        queued_count = 0
        failed_channels: list[str] = []

        if target_lower == "announcements":
            channel = guild.get_channel(settings.DISCORD_ANNOUNCEMENT_CHANNEL_ID)
            if not channel or not isinstance(channel, discord.TextChannel):
                raise RuntimeError("Announcements channel not found")

            blueteam_role = guild.get_role(settings.BLUETEAM_ROLE_ID)
            role_mention = blueteam_role.mention if blueteam_role else "@Blueteam"

            await channel.send(f"{role_mention}\n\n{message}")
            sent_count = 1
            logger.info(f"Broadcast to announcements by {sender}")

        elif target_lower == "all-teams":
            teams = [t async for t in Team.objects.filter(is_active=True).order_by("team_number")]
            for team in teams:
                result = await self._send_to_team_channel(guild, team, message, sender)
                if result == "sent":
                    sent_count += 1
                elif result == "queued":
                    queued_count += 1
                else:
                    failed_channels.append(f"Team {team.team_number:02d}")

        else:
            try:
                team_numbers = parse_team_range(target)
            except ValueError as e:
                raise ValueError(f"Invalid team range: {e}") from e

            for team_number in team_numbers:
                found_team = await Team.objects.filter(team_number=team_number).afirst()
                if not found_team:
                    failed_channels.append(f"Team {team_number:02d} (not found)")
                    continue

                result = await self._send_to_team_channel(guild, found_team, message, sender)
                if result == "sent":
                    sent_count += 1
                elif result == "queued":
                    queued_count += 1
                else:
                    failed_channels.append(f"Team {team_number:02d}")

        await log_to_ops_channel(
            self.bot,
            f"Broadcast by {sender}\n• Target: {target}\n• Sent: {sent_count}\n• Queued: {queued_count}",
        )

        logger.info(f"Broadcast complete: sent={sent_count}, queued={queued_count}, failed={len(failed_channels)}")
        return {"sent_count": sent_count, "queued_count": queued_count, "failed_count": len(failed_channels)}

    async def _send_to_team_channel(self, guild: discord.Guild, team: Team, message: str, sender: str) -> str:
        """Send message to a team's chat channel. Returns 'sent', 'queued', or 'failed'."""
        from core.models import QueuedAnnouncement

        try:
            chat_channel = team_chat_channel(guild, team)
            if chat_channel:
                await chat_channel.send(f"**Announcement from {sender}:**\n\n{message}")
                return "sent"
            else:
                await QueuedAnnouncement.objects.acreate(
                    team=team,
                    message=message,
                    sender_name=sender,
                )
                return "queued"
        except Exception as e:
            logger.exception(f"Failed to send to team {team.team_number}: {e}")
            return "failed"
