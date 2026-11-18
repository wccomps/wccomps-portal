"""Deliver the chat notices the portal queues to Zulip; core.task_queue polls, claims and retries them. A ticket's
messages go to its topic in its team's stream (chat.zulip.naming), saying what core.chat_notices says, as Markdown.

A Zulip refusal (ZulipError) or a failed connection (httpx.HTTPError) fails the attempt and is retried; a stream that
doesn't exist is reported and not retried."""

import logging
from collections.abc import Mapping
from datetime import datetime
from typing import TypedDict, assert_never

from asgiref.sync import sync_to_async
from django.conf import settings

from chat.zulip.client import ZulipClient, ZulipError
from chat.zulip.markup import fit, quiet, to_zulip, zulip_time
from chat.zulip.naming import team_broadcast_stream, team_stream, ticket_topic
from core.broadcast import AllTeams, Announcements, Teams, parse_broadcast_target, target_teams
from core.chat_notices import Card, Field, category_name, status_card, ticket_fields, ticket_update
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
from core.utils import announcement
from ticketing.models import Ticket, TicketComment
from ticketing.utils import relay_parts, ticket_url

logger = logging.getLogger(__name__)

# Zulip's default limit; the server cuts a longer message
ZULIP_MESSAGE_CHAR_LIMIT = 10000
STREAM_DOES_NOT_EXIST = "STREAM_DOES_NOT_EXIST"
ANNOUNCEMENT_TOPIC = "announcements"
STATUS_TOPIC = "competition status"
# teams: each team's number and the stream it lacks, such as "02 (team02)"
MISSING_STREAMS = "No Zulip stream for teams {teams}. Create the streams, then send it again to those teams."


class BroadcastState(TypedDict, total=False):
    """A broadcast's progress: the team numbers reached, and those refused for good (the number as a string, JSON's
    key) with why."""

    reached: list[int]
    refused: dict[str, str]


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
                await self._send(settings.ZULIP_OPS_STREAM, STATUS_TOPIC, _card(status_card(config)))
            case _:
                assert_never(payload)
        return None

    async def _send_ops(self, message: str) -> None:
        await self._send(settings.ZULIP_OPS_STREAM, settings.ZULIP_OPS_TOPIC, message)

    async def _send(self, stream: str, topic: str, content: str) -> None:
        """Post content to the topic, its times converted and cut to Zulip's limit; a stream that doesn't exist fails
        the task for good."""
        try:
            await self.client.send_stream_message(stream, topic, fit(to_zulip(content), ZULIP_MESSAGE_CHAR_LIMIT))
        except ZulipError as e:
            if e.code == STREAM_DOES_NOT_EXIST:
                raise PermanentTaskError(f"No Zulip stream {stream}: create it, then resend") from e
            raise

    async def _send_each(self, topic: str, sends: list[tuple[str, str]], progress: TaskProgress[StepsState]) -> None:
        """Post each (stream, content) to the topic, skipping those an earlier attempt sent, and beating the queue's
        heartbeat after each."""
        for n, (stream, content) in enumerate(sends):
            if n < progress.done:
                continue
            await self._send(stream, topic, content)
            await progress.advance()
            beat()

    async def _send_parts(self, ticket: Ticket, text: str, progress: TaskProgress[StepsState]) -> None:
        """Send text to the ticket's topic in at most ticketing.utils.relay_parts' few messages under Zulip's limit, the
        last pointing to the ticket's page when it is cut, skipping the parts an earlier attempt sent."""
        parts = relay_parts(to_zulip(text), ZULIP_MESSAGE_CHAR_LIMIT, ticket_url(ticket.ticket_number))
        stream = team_stream(ticket.team.team_number)
        await self._send_each(await ticket_topic(ticket), [(stream, part) for part in parts], progress)

    async def _ticket_created(self, payload: TicketCreated, progress: TaskProgress[StepsState]) -> None:
        """The ticket's summary in its topic, then, when ZULIP_STAFF_STREAM is set, an alert with its link in the staff
        stream; a retry skips the one already sent."""
        tickets = Ticket.objects.select_related("team", "category", "assigned_to")
        ticket = await _target(tickets, id=payload.ticket_id)
        topic = await ticket_topic(ticket)
        url = ticket_url(ticket.ticket_number)
        alert = f"New ticket [{ticket.ticket_number}]({url}) from {quiet(ticket.team.team_name)}: {quiet(ticket.title)}"
        sends = [(team_stream(ticket.team.team_number), _ticket_summary(ticket))]
        if settings.ZULIP_STAFF_STREAM:
            sends.append((settings.ZULIP_STAFF_STREAM, alert))
        await self._send_each(topic, sends, progress)

    async def _post_comment(self, payload: PostComment, progress: TaskProgress[StepsState]) -> None:
        ticket = await _target(Ticket.objects.select_related("team"), id=payload.ticket_id)
        comment = await _target(TicketComment.objects.select_related("author"), id=payload.comment_id)
        await self._send_parts(ticket, f"**{quiet(comment.author_name)}**\n{quiet(comment.comment_text)}", progress)

    async def _post_ticket_update(self, payload: PostTicketUpdate, progress: TaskProgress[StepsState]) -> None:
        ticket = await _target(Ticket.objects.select_related("team"), id=payload.ticket_id)
        match ticket_update(payload):
            case Card() as card:
                text = _card(card)
            case str(update):
                text = quiet(update)
        await self._send_parts(ticket, text, progress)

    async def _broadcast(self, payload: BroadcastMessage, progress: TaskProgress[BroadcastState]) -> dict[str, object]:
        """The announcements stream, or each target team's broadcast stream and topic (ZULIP_TEAM_BROADCAST_STREAM and
        ZULIP_TEAM_BROADCAST_TOPIC; core.broadcast.target_teams). A team without a stream is refused for good; those
        and the range's numbers with no team are reported to ops once every team is reached or refused. Any other
        failure is retried, skipping the teams recorded in progress."""
        if not payload.target or not payload.message:
            raise PermanentTaskError("Missing target or message in payload")
        try:
            target = parse_broadcast_target(payload.target)
        except ValueError as e:
            raise PermanentTaskError(f"Invalid team range: {e}") from e

        match target:
            case Announcements():
                await self._send(settings.ZULIP_ANNOUNCEMENTS_STREAM, ANNOUNCEMENT_TOPIC, quiet(payload.message))
                return {"stream": settings.ZULIP_ANNOUNCEMENTS_STREAM}
            case AllTeams() | Teams():
                teams, not_found = await target_teams(target)

        state = progress.state
        reached, refused = state.setdefault("reached", []), state.setdefault("refused", {})
        content = announcement(payload.sender, quiet(payload.message))
        for team in teams:
            number = team.team_number
            if number in reached or str(number) in refused:
                continue
            # A cancelled start scrubs and fails its broadcast: the teams not reached yet get nothing
            if not await progress.still_running():
                raise PermanentTaskError("The broadcast was stopped before it reached every team")
            try:
                topic = settings.ZULIP_TEAM_BROADCAST_TOPIC.format(number=number)
                await self._send(team_broadcast_stream(number), topic, content)
            except PermanentTaskError as e:
                refused[str(number)] = str(e)
            else:
                reached.append(number)
            await progress.saved()
            beat()

        missing = sorted(int(number) for number in refused)
        problems = []
        if not_found:
            problems.append(f"Not found: {', '.join(f'Team {number:02d}' for number in not_found)}")
        if missing:
            streams = ", ".join(f"{number:02d} ({team_broadcast_stream(number)})" for number in missing)
            problems.append(MISSING_STREAMS.format(teams=streams))
        if problems:
            await self._send_ops("\n".join([f"Broadcast from {payload.sender} to {payload.target}:", *problems]))
        return {"delivered": list(reached), "missing": missing, "not_found": not_found}


def _field_lines(fields: list[Field]) -> list[str]:
    """A line per field that isn't blank, its text with mentions silenced."""
    return [
        f"**{name}:** {zulip_time(value) if isinstance(value, datetime) else quiet(value)}"
        for name, value, _ in fields
        if value
    ]


def _card(card: Card) -> str:
    heading = f"**{card.title}: {card.headline}**" if card.headline else f"**{card.title}**"
    return "\n".join([heading, *_field_lines(card.fields)])


def _mentions(team_number: int) -> str:
    """The team's and staff's user group mentions, those set."""
    groups = [
        settings.ZULIP_TEAM_MENTION.format(number=team_number) if settings.ZULIP_TEAM_MENTION else "",
        settings.ZULIP_STAFF_MENTION,
    ]
    return " ".join(f"@*{group}*" for group in groups if group)


def _ticket_summary(ticket: Ticket) -> str:
    """The ticket's facts (core.chat_notices.ticket_fields) under a line mentioning its team and staff; the
    description takes what is left of one message."""
    first = f"New ticket {ticket.ticket_number}: {quiet(ticket.title)}"
    lines = [f"{mentions} {first}" if (mentions := _mentions(ticket.team.team_number)) else first]
    lines += _field_lines([*ticket_fields(ticket), Field("Category", category_name(ticket))])
    summary = to_zulip("\n".join(lines))
    if ticket.description:
        room = ZULIP_MESSAGE_CHAR_LIMIT - len(summary) - 2
        summary += "\n\n" + fit(to_zulip(quiet(ticket.description)), room)
    return summary
