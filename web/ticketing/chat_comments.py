"""A message in a ticket's chat thread or topic becomes the ticket's comment the same way on every chat backend:
counted once against the rate limit, saved once, and queued for the other chat backends. Its edits and deletes carry
over to the comment and go no further (the copies elsewhere keep their text). Each backend decides first who may
write to the ticket, and stores the message's files itself."""

import logging
from dataclasses import dataclass

from django.contrib.auth.models import User
from django.db import transaction

from core.models import QueuedTask
from core.queued_tasks import PostComment
from core.task_backends import TaskBackend
from ticketing.models import CommentRateLimit, Ticket, TicketAttachment, TicketComment
from ticketing.utils import storable

logger = logging.getLogger(__name__)

DELETED_TEXT = "[Message deleted]"

# The TicketComment column holding the id of the chat message a comment came from
_MESSAGE_ID_FIELD = {TaskBackend.DISCORD: "discord_message_id", TaskBackend.ZULIP: "zulip_message_id"}


@dataclass(frozen=True)
class Recorded:
    refused: str = ""  # why the rate limit refused the message; nothing was saved
    store_files: bool = False  # the message has files not yet saved, and the caller stores them now


def _files_saved(origin: TaskBackend, message_id: int) -> bool:
    """Whether the message's files are already the ticket's attachments: only a Zulip message's record its id."""
    return origin == TaskBackend.ZULIP and TicketAttachment.objects.filter(zulip_message_id=message_id).exists()


def record_message(
    ticket: Ticket,
    origin: TaskBackend,
    *,
    message_id: int,
    actor_key: str,
    author: User | None,
    text: str,
    has_files: bool,
) -> Recorded:
    """Save a chat message's text as the ticket's comment and queue it for the other chat backends, counting the
    message once against the rate limit (text and files together). A message whose text and files are already saved
    (an event delivered again) is passed over, and so is one with neither."""
    field, text = _MESSAGE_ID_FIELD[origin], storable(text)
    with transaction.atomic():
        new_comment = bool(text) and not TicketComment.objects.filter(**{field: message_id}).exists()
        new_files = has_files and not _files_saved(origin, message_id)
        if not (new_comment or new_files):
            return Recorded()
        allowed, reason = CommentRateLimit.check_rate_limit(ticket.id, actor_key)
        if not allowed:
            return Recorded(refused=reason)
        CommentRateLimit.objects.create(ticket=ticket, actor_key=actor_key)
        comment = None
        if new_comment:
            comment = TicketComment.objects.create(
                ticket=ticket, author=author, comment_text=text, **{field: message_id}
            )
            # The other chat backends post it; the one it came from already shows it
            QueuedTask.enqueue(PostComment(ticket_id=ticket.id, comment_id=comment.id), origin=origin)
    if comment is not None:
        logger.info(
            f"Saved {origin.label} message {message_id} as comment {comment.id} on ticket {ticket.ticket_number}"
        )
    return Recorded(store_files=new_files)


def apply_edit(origin: TaskBackend, message_id: int, text: str) -> None:
    """A chat message's new text onto its comment, if it has one."""
    if TicketComment.objects.filter(**{_MESSAGE_ID_FIELD[origin]: message_id}).update(comment_text=storable(text)):
        logger.info(f"Synced the edit of {origin.label} message {message_id} to its comment")


def apply_delete(origin: TaskBackend, message_ids: list[int]) -> None:
    """Mark the comments of deleted chat messages deleted."""
    if TicketComment.objects.filter(**{f"{_MESSAGE_ID_FIELD[origin]}__in": message_ids}).update(
        comment_text=DELETED_TEXT
    ):
        logger.info(f"Marked the comments of deleted {origin.label} messages {message_ids} deleted")
