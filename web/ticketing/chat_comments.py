"""A message in a ticket's chat thread or topic becomes the ticket's comment the same way on every chat backend:
counted once against the rate limit, saved once, and queued for the other chat backends. Its edits and deletes carry
over to the comment and go no further (the copies elsewhere keep their text); an edit only while the comment's author
may still write to the ticket. Each backend decides first who may write to the ticket, and stores the message's files
itself."""

import logging

from django.contrib.auth.models import User
from django.db import transaction

from core.models import QueuedTask
from core.queued_tasks import PostComment
from core.task_backends import TaskBackend
from ticketing.access import ticket_open_to_user
from ticketing.models import CommentRateLimit, Ticket, TicketComment
from ticketing.utils import storable

logger = logging.getLogger(__name__)

DELETED_TEXT = "[Message deleted]"

# The TicketComment column holding the id of the chat message a comment came from
_MESSAGE_ID_FIELD = {TaskBackend.DISCORD: "discord_message_id", TaskBackend.ZULIP: "zulip_message_id"}


def record_message(
    ticket: Ticket,
    origin: TaskBackend,
    *,
    message_id: int,
    actor_key: str,
    author: User | None,
    text: str,
    has_files: bool,  # the message has files not yet saved, which the caller stores unless refused
) -> str:
    """Save a chat message's text as the ticket's comment and queue it for the other chat backends, counting the
    message once against the rate limit (text and files together); why the rate limit refused it (nothing saved), or
    "". A message with no new text or files (an event delivered again) is passed over."""
    field, text = _MESSAGE_ID_FIELD[origin], storable(text)
    with transaction.atomic():
        new_comment = bool(text) and not TicketComment.objects.filter(**{field: message_id}).exists()
        if not (new_comment or has_files):
            return ""
        allowed, reason = CommentRateLimit.check_rate_limit(ticket.id, actor_key)
        if not allowed:
            return reason
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
    return ""


def apply_edit(origin: TaskBackend, message_id: int, text: str) -> None:
    """A chat message's new text onto its comment, if it has one whose author may still write to the ticket
    (ticketing.access.ticket_open_to_user): an author since disabled or moved off the team changes nothing, and nor
    does an edit to a comment with no portal author (an older or unlinked row)."""
    comments = TicketComment.objects.select_related("ticket", "author__usergroups")
    comment = comments.filter(**{_MESSAGE_ID_FIELD[origin]: message_id}).first()
    if comment is None or comment.author is None or not ticket_open_to_user(comment.ticket, comment.author):
        return
    TicketComment.objects.filter(pk=comment.pk).update(comment_text=storable(text))
    logger.info(f"Synced the edit of {origin.label} message {message_id} to its comment")


def apply_delete(origin: TaskBackend, message_ids: list[int]) -> None:
    """Mark the comments of deleted chat messages deleted."""
    if TicketComment.objects.filter(**{f"{_MESSAGE_ID_FIELD[origin]}__in": message_ids}).update(
        comment_text=DELETED_TEXT
    ):
        logger.info(f"Marked the comments of deleted {origin.label} messages {message_ids} deleted")
