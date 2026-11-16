"""Messages in a ticket's Zulip topic become the ticket's comments, and their edits and deletes carry over to the
comment. A comment goes on to the other chat backends; an edit or delete doesn't (the copy there keeps its text).

The event fields read here are Zulip's JSON: anything can be missing or of another type, so each reader checks what it
uses and passes over an event it can't read."""

import logging
from collections.abc import Mapping
from dataclasses import dataclass

import httpx
from asgiref.sync import sync_to_async
from django.conf import settings
from django.contrib.auth.models import User
from django.db import transaction

from chat.zulip.client import ZulipClient, ZulipError
from chat.zulip.markup import quiet
from chat.zulip.naming import team_for_stream, ticket_number_from_topic
from core.models import QueuedTask
from core.queued_tasks import PostComment
from core.task_backends import TaskBackend
from ticketing.models import CommentRateLimit, Ticket, TicketComment
from ticketing.utils import storable

logger = logging.getLogger(__name__)

DELETED_TEXT = "[Message deleted]"
# Zulip's ids fit a BigIntegerField; anything else in an event isn't one
_ID_LIMIT = 2**63


@dataclass(frozen=True)
class IncomingMessage:
    message_id: int
    stream: str
    topic: str
    sender_id: int
    sender_email: str
    sender_name: str
    content: str


def _id(value: object) -> int | None:
    return value if type(value) is int and 0 < value < _ID_LIMIT else None


def parse_message_event(event: Mapping[str, object]) -> IncomingMessage | None:
    """The stream message a "message" event carries; None for a direct message, an event it can't read, or the bot's
    own message (a notice it posted)."""
    message = event.get("message")
    if event.get("type") != "message" or not isinstance(message, dict) or message.get("type") != "stream":
        return None
    message_id, sender_id = _id(message.get("id")), _id(message.get("sender_id"))
    fields = ("display_recipient", "subject", "sender_email", "sender_full_name", "content")
    texts = [message.get(key) for key in fields]
    if message_id is None or sender_id is None or not all(isinstance(text, str) for text in texts):
        return None
    stream, topic, email, name, content = (str(text) for text in texts)
    bot_email = settings.ZULIP_BOT_EMAIL
    if bot_email and email.lower() == bot_email.lower():
        return None
    return IncomingMessage(message_id, stream, topic, sender_id, email, name, content)


def edited_message(event: Mapping[str, object]) -> tuple[int, str] | None:
    """(message id, new text) of an "update_message" event that changed a message's text; None for one that only
    moved it or that can't be read."""
    message_id, content = _id(event.get("message_id")), event.get("content")
    if event.get("type") != "update_message" or message_id is None or not isinstance(content, str):
        return None
    return message_id, content


def deleted_message_ids(event: Mapping[str, object]) -> list[int]:
    """The messages a "delete_message" event deletes: one (message_id), or several (message_ids) for a client that
    takes them in bulk."""
    if event.get("type") != "delete_message":
        return []
    ids = event.get("message_ids")
    found = [_id(value) for value in ids] if isinstance(ids, list) else [_id(event.get("message_id"))]
    return [message_id for message_id in found if message_id is not None]


def find_ticket(stream: str, topic: str) -> Ticket | None:
    """The ticket a topic names, when it is the stream's team's own: a topic in team 3's stream naming team 5's ticket
    finds nothing, as another team's record is never written to."""
    team_number, ticket_number = team_for_stream(stream), ticket_number_from_topic(topic)
    if team_number is None or ticket_number is None:
        return None
    return Ticket.objects.filter(team__team_number=team_number, ticket_number=ticket_number).first()


def _portal_user(email: str) -> User | None:
    """The one portal user with the sender's email (as when the region's Zulip signs in through Authentik); None when
    there is none, or more than one to tell apart."""
    if not email:
        return None
    users = list(User.objects.filter(email__iexact=email)[:2])
    return users[0] if len(users) == 1 else None


def _record(msg: IncomingMessage) -> str | None:
    """Save the message as its ticket's comment and queue it for the other chat backends; the reason when the rate
    limit refuses it. A message seen before (a redelivered event), outside a ticket's topic or without text is
    passed over."""
    content = storable(msg.content)
    ticket = find_ticket(msg.stream, msg.topic)
    if ticket is None or not content or TicketComment.objects.filter(zulip_message_id=msg.message_id).exists():
        return None
    actor_key = f"zulip:{msg.sender_id}"
    allowed, reason = CommentRateLimit.check_rate_limit(ticket.id, actor_key)
    if not allowed:
        return reason
    author = _portal_user(msg.sender_email)
    with transaction.atomic():
        CommentRateLimit.objects.create(ticket=ticket, actor_key=actor_key)
        comment = TicketComment.objects.create(
            ticket=ticket,
            author=author,
            external_author="" if author else storable(msg.sender_name)[:255],
            comment_text=content,
            zulip_message_id=msg.message_id,
        )
        # The other chat backends post it; the topic already shows it
        QueuedTask.enqueue(PostComment(ticket_id=ticket.id, comment_id=comment.id), origin=TaskBackend.ZULIP)
    logger.info(f"Saved Zulip message {msg.message_id} as comment {comment.id} on ticket {ticket.ticket_number}")
    return None


async def save_message(client: ZulipClient, msg: IncomingMessage) -> None:
    """Turn a message in a ticket's topic into the ticket's comment. Over the rate limit, nothing is saved and the
    topic is told (Zulip keeps the message: the bot can't delete it)."""
    refused = await sync_to_async(_record)(msg)
    if refused is None:
        return
    logger.warning(f"Rate limit refused Zulip message {msg.message_id} from user {msg.sender_id}: {refused}")
    reply = f"{quiet(msg.sender_name)}: {refused}; this message was not added to the ticket. Please slow down."
    try:
        await client.send_stream_message(msg.stream, msg.topic, reply)
    except ZulipError, httpx.HTTPError:
        logger.exception(f"Could not tell {msg.stream} > {msg.topic} that a message was refused")


async def apply_edit(event: Mapping[str, object]) -> None:
    """A message's new text onto its comment. Not passed on to the other chat backends."""
    if (edit := edited_message(event)) is None:
        return
    message_id, content = edit
    if await TicketComment.objects.filter(zulip_message_id=message_id).aupdate(comment_text=storable(content)):
        logger.info(f"Synced the edit of Zulip message {message_id} to its comment")


async def apply_delete(event: Mapping[str, object]) -> None:
    """Mark the deleted messages' comments deleted. Not passed on to the other chat backends."""
    if not (message_ids := deleted_message_ids(event)):
        return
    if await TicketComment.objects.filter(zulip_message_id__in=message_ids).aupdate(comment_text=DELETED_TEXT):
        logger.info(f"Marked the comments of deleted Zulip messages {message_ids} deleted")


async def handle_event(client: ZulipClient, event: Mapping[str, object]) -> None:
    """One event from the queue: a message, an edit or a delete; any other (Zulip's heartbeat) needs nothing."""
    match event.get("type"):
        case "message":
            if (msg := parse_message_event(event)) is not None:
                await save_message(client, msg)
        case "update_message":
            await apply_edit(event)
        case "delete_message":
            await apply_delete(event)
