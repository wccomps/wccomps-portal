"""Deliver the chat notices the portal queues to Zulip; core.task_queue polls, claims and retries them. A ticket's
messages go to its topic in its team's stream (chat.zulip.naming), with the wording the Discord thread shows.

A Zulip refusal (ZulipError) or a failed connection (httpx.HTTPError) fails the attempt and is retried; a stream that
doesn't exist is reported and not retried."""

import logging
from collections.abc import Mapping
from typing import Never, NoReturn, TypedDict, assert_never

from asgiref.sync import sync_to_async
from django.conf import settings
from django.urls import reverse

from bot.utils import announcement, split_message
from chat.zulip.client import ZulipClient, ZulipError
from chat.zulip.markup import quiet, to_zulip
from chat.zulip.naming import team_stream, ticket_topic
from core.broadcast import AllTeams, Announcements, Teams, parse_broadcast_target
from core.heartbeat import QUEUE_ZULIP, beat, beat_for
from core.models import CompetitionConfig, QueuedTask
from core.queued_tasks import (
    BroadcastMessage,
    ChatNotice,
    CompetitionStatusChanged,
    LogToChannel,
    PermanentTaskError,
    PostComment,
    PostTicketUpdate,
    TicketCreated,
)
from core.task_backends import TaskBackend
from core.task_queue import QueueProcessor, StepsState, TaskProgress, _target
from team.models import MAX_TEAMS, Team
from ticketing.models import Ticket, TicketComment

logger = logging.getLogger(__name__)

# Zulip's default limit; the server cuts a longer message
ZULIP_MESSAGE_CHAR_LIMIT = 10000
STREAM_DOES_NOT_EXIST = "STREAM_DOES_NOT_EXIST"
ANNOUNCEMENT_TOPIC = "announcements"
STATUS_TOPIC = "competition status"
MISSING_STREAMS = (
    "Broadcast from {sender} to {target}: no Zulip stream for teams {teams}. Create the streams, then send it again "
    "to those teams."
)


class BroadcastState(TypedDict, total=False):
    """A broadcast's progress: the team numbers reached, and those refused for good (the number as a string, JSON's
    key) with why."""

    reached: list[int]
    refused: dict[str, str]


def _fit(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


class ZulipQueueProcessor(QueueProcessor[ChatNotice]):
    backend = TaskBackend.ZULIP
    heartbeat_name = QUEUE_ZULIP

    def __init__(self, client: ZulipClient) -> None:
        # on_start binds the loop's task to the heartbeat, so beat() in a broadcast advances it too
        super().__init__(on_start=beat_for, on_pass=beat)
        self.client = client

    async def report(self, message: str) -> None:
        try:
            await self._send_ops(message)
        except Exception:
            logger.exception(f"Could not post to the Zulip ops stream: {message}")

    async def dispatch(self, payload: ChatNotice, task: QueuedTask) -> Mapping[str, object] | None:
        match payload:
            case TicketCreated():
                await self._ticket_created(payload, TaskProgress.of(task))
            case PostComment():
                await self._post_comment(payload, TaskProgress.of(task))
            case PostTicketUpdate():
                await self._post_ticket_update(payload, TaskProgress.of(task))
            case BroadcastMessage():
                return await self._broadcast(payload, TaskProgress.of(task))
            case LogToChannel():
                if not payload.message:
                    raise PermanentTaskError("Missing message in payload")
                await self._send_ops(payload.message)
            case CompetitionStatusChanged():
                config = await sync_to_async(CompetitionConfig.get_config)()
                await self._send(settings.ZULIP_OPS_STREAM, STATUS_TOPIC, _status(config))
            case _:
                _not_a_zulip_task(payload)
        return None

    async def _send_ops(self, message: str) -> None:
        await self._send(settings.ZULIP_OPS_STREAM, settings.ZULIP_OPS_TOPIC, _fit(message, ZULIP_MESSAGE_CHAR_LIMIT))

    async def _send(self, stream: str, topic: str, content: str) -> None:
        """Post to the topic; a stream that doesn't exist fails the task for good."""
        try:
            await self.client.send_stream_message(stream, topic, to_zulip(content))
        except ZulipError as e:
            if e.code == STREAM_DOES_NOT_EXIST:
                raise PermanentTaskError(f"No Zulip stream {stream}: create it, then resend") from e
            raise

    async def _send_parts(self, stream: str, topic: str, text: str, progress: TaskProgress[StepsState]) -> None:
        """Send text in as many messages as Zulip's limit takes, skipping the parts an earlier attempt sent."""
        for n, part in enumerate(split_message(text, ZULIP_MESSAGE_CHAR_LIMIT)):
            if n < progress.done:
                continue
            await self._send(stream, topic, part)
            await progress.advance()

    async def _ticket_created(self, payload: TicketCreated, progress: TaskProgress[StepsState]) -> None:
        """The ticket's summary in its topic, then an alert with its link in the staff stream; a retry skips the one
        already sent."""
        ticket = await _target(Ticket.objects.select_related("team", "category"), id=payload.ticket_id)
        topic = ticket_topic(ticket)
        url = settings.BASE_URL.rstrip("/") + reverse("ticket_detail", args=[ticket.ticket_number])
        title = quiet(ticket.title)
        alert = f"New ticket [{ticket.ticket_number}]({url}) from {quiet(ticket.team.team_name)}: {title}"
        sends = [
            (team_stream(ticket.team.team_number), _ticket_summary(ticket)),
            (settings.ZULIP_STAFF_STREAM, alert),
        ]
        for n, (stream, content) in enumerate(sends):
            if n < progress.done:
                continue
            await self._send(stream, topic, content)
            await progress.advance()

    async def _post_comment(self, payload: PostComment, progress: TaskProgress[StepsState]) -> None:
        ticket = await _target(Ticket.objects.select_related("team"), id=payload.ticket_id)
        comment = await _target(TicketComment.objects.select_related("author"), id=payload.comment_id)
        author = (comment.author.username if comment.author else "") or "Unknown"
        # A web comment has no length limit; send all of it, in as many messages as it takes
        text = f"**{author}**\n{quiet(comment.comment_text)}"
        await self._send_parts(team_stream(ticket.team.team_number), ticket_topic(ticket), text, progress)

    async def _post_ticket_update(self, payload: PostTicketUpdate, progress: TaskProgress[StepsState]) -> None:
        ticket = await _target(Ticket.objects.select_related("team"), id=payload.ticket_id)
        actor = payload.actor
        match payload.action:
            case "resolved":
                text = f"**Ticket Resolved**\nResolved By: {actor}\nPoints Charged: {payload.points_charged}"
                if payload.resolution_notes:
                    text += f"\nResolution Notes: {quiet(payload.resolution_notes)}"
            case "claimed":
                text = f"Ticket claimed by **{actor}**"
            case "assigned":
                text = f"Ticket assigned to **{payload.assignee}** by **{actor}**"
            case "unclaimed":
                text = f"Ticket unclaimed by **{actor}**"
            case "cancelled":
                text = f"Ticket cancelled by **{actor}**"
            case "reopened":
                text = f"Ticket reopened by **{actor}**"
                if payload.reason:
                    text += f"\nReason: {quiet(payload.reason)}"
            case "points_adjusted":
                text = f"Ticket points adjusted to **{payload.points_charged}** by **{actor}**"
                if payload.reason:
                    text += f"\nReason: {quiet(payload.reason)}"
            case action:
                text = f"Ticket updated: {action} by **{actor}**"
        await self._send_parts(team_stream(ticket.team.team_number), ticket_topic(ticket), text, progress)

    async def _broadcast(self, payload: BroadcastMessage, progress: TaskProgress[BroadcastState]) -> dict[str, object]:
        """The announcements stream, or each target team's stream. A team without a stream is refused for good and
        reported to ops once every team is reached or refused; any other failure is retried, skipping the teams
        recorded in progress."""
        if not payload.target or not payload.message:
            raise PermanentTaskError("Missing target or message in payload")
        try:
            target = parse_broadcast_target(payload.target)
        except ValueError as e:
            raise PermanentTaskError(f"Invalid team range: {e}") from e

        teams = Team.objects.order_by("team_number").values_list("team_number", flat=True)
        match target:
            case Announcements():
                await self._send(settings.ZULIP_ANNOUNCEMENTS_STREAM, ANNOUNCEMENT_TOPIC, quiet(payload.message))
                return {"stream": settings.ZULIP_ANNOUNCEMENTS_STREAM}
            case AllTeams():
                numbers = [n async for n in teams.filter(is_active=True)]
            case Teams(wanted):
                # As the Discord broadcast: the teams of the range that exist, active or not
                numbers = [n async for n in teams.filter(team_number__in=wanted)]
            case _:
                assert_never(target)

        state = progress.state
        reached, refused = state.setdefault("reached", []), state.setdefault("refused", {})
        content = announcement(payload.sender, quiet(payload.message))
        for number in numbers:
            if number in reached or str(number) in refused:
                continue
            # A cancelled start scrubs and fails its broadcast: the teams not reached yet get nothing
            if not await progress.still_running():
                raise PermanentTaskError("The broadcast was stopped before it reached every team")
            stream = team_stream(number)
            try:
                await self.client.send_stream_message(stream, ANNOUNCEMENT_TOPIC, to_zulip(content))
            except ZulipError as e:
                if e.code != STREAM_DOES_NOT_EXIST:
                    raise
                refused[str(number)] = f"no stream {stream}"
            else:
                reached.append(number)
            await progress.saved()
            beat()

        missing = sorted(int(number) for number in refused)
        if missing:
            teams_text = ", ".join(f"{number:02d}" for number in missing)
            await self._send_ops(MISSING_STREAMS.format(sender=payload.sender, target=payload.target, teams=teams_text))
        return {"delivered": list(reached), "missing": missing}


def _not_a_zulip_task(payload: Never) -> NoReturn:
    """A row of another backend's type, despite the queue's check: no Zulip handler runs it. Typed Never, so mypy
    fails the match above when a ChatNotice has no case."""
    raise PermanentTaskError(f"{type(payload).__name__} is not a Zulip task")


def _mentions(team_number: int) -> str:
    """The team's and staff's user group mentions, those set."""
    groups = [
        settings.ZULIP_TEAM_MENTION.format(number=team_number) if settings.ZULIP_TEAM_MENTION else "",
        settings.ZULIP_STAFF_MENTION,
    ]
    return " ".join(f"@*{group}*" for group in groups if group)


def _ticket_summary(ticket: Ticket) -> str:
    """The ticket as its Discord thread's message shows it, mentioning its team and staff; the description takes
    what is left of one message."""
    first = f"New ticket {ticket.ticket_number}: {quiet(ticket.title)}"
    lines = [f"{mentions} {first}" if (mentions := _mentions(ticket.team.team_number)) else first]
    cat = ticket.category
    facts = [
        ("Team", f"{ticket.team.team_name} (#{ticket.team.team_number})"),
        ("Category", cat.display_name if cat else f"Category {ticket.category_id}"),
        ("Hostname", ticket.hostname),
        ("Service", ticket.service_name),
        ("IP Address", ticket.ip_address or ""),
    ]
    if cat and cat.variable_points:
        facts.append(("Point Impact", "Variable"))
    elif cat and cat.points > 0:
        facts.append(("Point Impact", f"{cat.points} points"))
    lines += [f"**{name}:** {quiet(value)}" for name, value in facts if value]
    summary = "\n".join(lines)
    if ticket.description:
        room = ZULIP_MESSAGE_CHAR_LIMIT - len(summary) - 2
        summary += "\n\n" + _fit(quiet(ticket.description), room)
    return summary


def _status(config: CompetitionConfig) -> str:
    """The facts the Discord status channel shows (bot.competition_actions.update_status_channel)."""
    if config.applications_enabled:
        status = "RUNNING"
    elif config.schedule_will_start():
        status = "SCHEDULED"
    else:
        status = "STOPPED"
    lines = [f"**Competition Status: {status}**"]
    if config.competition_start_time:
        lines.append(f"Scheduled Start: <t:{int(config.competition_start_time.timestamp())}:F>")
    if config.competition_end_time:
        lines.append(f"Scheduled End: <t:{int(config.competition_end_time.timestamp())}:F>")
    if config.controlled_applications:
        lines.append(f"Controlled Applications: {', '.join(config.controlled_applications)}")
    account_status = "Enabled" if config.applications_enabled else "Disabled"
    lines.append(f"Team Accounts: {account_status} ({MAX_TEAMS} teams)")
    return "\n".join(lines)
