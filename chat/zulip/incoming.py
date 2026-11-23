"""Messages in a ticket's Zulip topic become the ticket's comments (ticketing.chat_comments, as a Discord thread's
do), and their edits and deletes carry over to the comment. Only a sender whose real Zulip email (as Zulip gives it to
the bot) is the portal account of one of the ticket's team or of ticket staff writes to a ticket; anyone else is told
so in the topic. Files uploaded with a message are stored on the ticket as its attachments, as a Discord thread's are;
they stay out of the comment and aren't posted to the other chat backends. A topic renamed in Zulip (resolved,
retitled) to a name that still starts with its ticket's number becomes the ticket's stored topic (Ticket.zulip_topic),
so the portal's later messages follow it.

The event fields read here are Zulip's JSON: anything can be missing or of another type, so each reader checks what it
uses and passes over an event it can't read."""

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TypeGuard

import httpx
from asgiref.sync import sync_to_async
from django.conf import settings
from django.contrib.auth.models import User

from chat.zulip.client import UPLOAD_PREFIX, ZulipClient, ZulipError, is_upload_path
from chat.zulip.markup import quiet
from chat.zulip.naming import TOPIC_CHAR_LIMIT, team_for_stream, ticket_number_from_topic
from core.task_backends import TaskBackend
from ticketing import chat_comments
from ticketing.access import ticket_open_to_user
from ticketing.models import CommentRateLimit, Ticket, TicketAttachment

logger = logging.getLogger(__name__)

# Zulip's ids fit a BigIntegerField; anything else in an event isn't one
_ID_LIMIT = 2**63
# [name](/user_uploads/...): how Zulip's raw Markdown links a file uploaded with the message
_UPLOAD_LINK = re.compile(r"\[([^\]\n]*)\]\((" + re.escape(UPLOAD_PREFIX) + r"[^)\s]+)\)")
# Discord's cap on a message's attachments: a Zulip message stores no more files than a Discord one can carry
MAX_UPLOADS = 10
NOT_LINKED = (
    "only the team's members and staff linked to the portal by their Zulip email can add to a ticket; this message "
    "was not added to it."
)
NOT_CHECKED = (
    "the portal could not check who sent this message, so it was not added to the ticket; please send it again."
)
# Realm of Zulip's system bots (e.g. the Notification Bot's "marked this topic as resolved"); messages carry it as
# sender_realm_str (Zulip's zerver/lib/message_cache.py; SYSTEM_BOT_REALM in zproject/default_settings.py)
SYSTEM_BOT_REALM = "zulipinternal"


@dataclass(frozen=True)
class IncomingMessage:
    message_id: int
    stream: str
    topic: str
    sender_id: int
    sender_name: str
    content: str


def _id(value: object) -> int | None:
    return value if type(value) is int and 0 < value < _ID_LIMIT else None


def _all_text(values: tuple[object, ...]) -> TypeGuard[tuple[str, ...]]:
    return all(isinstance(value, str) for value in values)


def parse_message_event(event: Mapping[str, object]) -> IncomingMessage | None:
    """The stream message a "message" event carries; None for a direct message, an event it can't read, the bot's
    own message (a notice it posted), or one from Zulip's system bots."""
    message = event.get("message")
    if event.get("type") != "message" or not isinstance(message, dict) or message.get("type") != "stream":
        return None
    message_id, sender_id = _id(message.get("id")), _id(message.get("sender_id"))
    fields = ("display_recipient", "subject", "sender_email", "sender_full_name", "content")
    texts = tuple(message.get(key) for key in fields)
    if message_id is None or sender_id is None or not _all_text(texts):
        return None
    stream, topic, email, name, content = texts
    if email.lower() == settings.ZULIP_BOT_EMAIL.lower() or message.get("sender_realm_str") == SYSTEM_BOT_REALM:
        return None
    return IncomingMessage(message_id, stream, topic, sender_id, name, content)


def edited_message(event: Mapping[str, object]) -> tuple[int, str] | None:
    """(message id, new text) of an "update_message" event that changed a message's text; None for one that only
    moved it or that can't be read."""
    message_id, content = _id(event.get("message_id")), event.get("content")
    if event.get("type") != "update_message" or message_id is None or not isinstance(content, str):
        return None
    return message_id, content


def renamed_topic(event: Mapping[str, object]) -> tuple[str, str, str] | None:
    """(stream, old topic, new topic) of an "update_message" event that renamed a topic within its stream, as resolving
    or moving it in Zulip does; None for one that moved a single message (the topic keeps the rest), moved messages
    to another stream, names a topic the ticket can't store, or can't be read."""
    moved_on = event.get("propagate_mode") in ("change_later", "change_all")
    if event.get("type") != "update_message" or not moved_on or "new_stream_id" in event:
        return None
    texts = (event.get("stream_name"), event.get("orig_subject"), event.get("subject"))
    if not _all_text(texts):
        return None
    stream, old, new = texts
    if old == new or not 0 < len(new) <= TOPIC_CHAR_LIMIT or "\x00" in new:
        return None
    return stream, old, new


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
    users = list(User.objects.select_related("usergroups").filter(email__iexact=email)[:2])
    return users[0] if len(users) == 1 else None


def upload_links(content: str) -> list[tuple[str, str]]:
    """(name, path) of each file a message's text links as uploaded to this Zulip, once per path (without a query or
    fragment, which fetch the same file); a link anywhere else, or one whose path isn't plainly an upload
    (chat.zulip.client.is_upload_path), is passed over."""
    found: dict[str, str] = {}
    for match in _UPLOAD_LINK.finditer(content):
        name, link = match.groups()
        path = re.split(r"[?#]", link, maxsplit=1)[0]
        if is_upload_path(path):
            found.setdefault(path, name)
    return [(name, path) for path, name in found.items()]


def comment_text(content: str) -> str:
    """A message's text as its comment stores it: without its upload links (the files are the ticket's attachments),
    so a message that only carries files has none, as a Discord message with only attachments has none."""
    return _UPLOAD_LINK.sub("", content).strip()


@dataclass(frozen=True)
class _Recorded:
    refused: str = ""  # what to tell the sender: nothing was saved
    # The ticket to store the message's uploads on and the portal account they're credited to; None when none to store
    store_on: tuple[Ticket, User] | None = None


def _record(msg: IncomingMessage, ticket: Ticket, email: str, has_uploads: bool) -> _Recorded:
    """Record a message in its ticket's topic through ticketing.chat_comments, when its sender (by the email Zulip
    gives the bot for them) may write to the ticket."""
    author = _portal_user(email)
    if author is None or not ticket_open_to_user(ticket, author):
        logger.warning(f"Zulip message {msg.message_id} from {msg.sender_id} not added: no portal account may write")
        return _Recorded(refused=NOT_LINKED)
    has_files = has_uploads and not TicketAttachment.objects.filter(zulip_message_id=msg.message_id).exists()
    refused = chat_comments.record_message(
        ticket,
        TaskBackend.ZULIP,
        message_id=msg.message_id,
        actor_key=CommentRateLimit.user_key(author),
        author=author,
        text=comment_text(msg.content),
        has_files=has_files,
    )
    if refused:
        logger.warning(f"Refused Zulip message {msg.message_id} from {msg.sender_id}: {refused}")
        return _Recorded(refused=f"{refused}. This message was not added to the ticket.")
    return _Recorded(store_on=(ticket, author) if has_files else None)


async def _reply(msg: IncomingMessage, client: ZulipClient, text: str, about: str) -> None:
    """Tell the message's sender something in its topic; about names it in the log when Zulip can't take it."""
    try:
        await client.send_stream_message(msg.stream, msg.topic, f"{quiet(msg.sender_name)}: {text}")
    except ZulipError, httpx.HTTPError:
        logger.exception(f"Could not tell {msg.stream} > {msg.topic} that {about}")


async def _store_uploads(
    client: ZulipClient, msg: IncomingMessage, ticket: Ticket, author: User, uploads: list[tuple[str, str]]
) -> None:
    """Store each upload on the ticket, credited to the sender's portal account and typed from its bytes as a web
    upload is, and react with a paperclip once one is stored. A file over TicketAttachment's size limit, or one that
    can't be fetched or stored, is replied to in the topic in Discord's words and the rest go on; once the ticket takes
    no more (TicketAttachment.store) that is replied and the rest are left out."""
    stored = False
    for name, path in uploads:
        try:
            data = await client.fetch_upload(path, TicketAttachment.MAX_SIZE + 1)
            if problem := TicketAttachment.too_big(len(data)):
                await _reply(msg, client, f"{problem}. Please use a file sharing service.", "a file was too big")
                continue
            attachment = TicketAttachment.with_file(
                data,
                name,
                ticket=ticket,
                uploaded_by=author.username,
                zulip_message_id=msg.message_id,
            )
            if problem := await sync_to_async(attachment.store)():
                await _reply(msg, client, f"{problem}. Files from here on were not added.", "a file was refused")
                break
        except Exception:
            logger.exception(f"Failed to store an attachment of Zulip message {msg.message_id} for ticket #{ticket.id}")
            await _reply(msg, client, "Failed to store an attachment. Please try again.", "a file was not stored")
            continue
        stored = True
        logger.info(f"Stored attachment {attachment.filename} for ticket #{ticket.id}")
    if stored:
        try:
            await client.add_reaction(msg.message_id, "paperclip")
        except ZulipError, httpx.HTTPError:
            logger.exception(f"Could not react to Zulip message {msg.message_id}")


async def save_message(client: ZulipClient, msg: IncomingMessage) -> None:
    """Turn a message in a ticket's topic into the ticket's comment and its first MAX_UPLOADS uploads into the
    ticket's attachments; a message outside a ticket's topic is passed over. The sender is matched by the email Zulip
    gives the bot for them (ZulipClient.get_user_email), not the message's sender_email, a placeholder unless the
    sender shows their address to everyone. A sender who may not write to the ticket, or one over the rate limit, gets
    nothing saved and is told in the topic (the message stays in Zulip); so is one Zulip couldn't be asked about."""
    ticket = await sync_to_async(find_ticket)(msg.stream, msg.topic)
    if ticket is None:
        return
    try:
        email = await client.get_user_email(msg.sender_id)
    except ZulipError, httpx.HTTPError:
        logger.exception(f"Could not look up the sender of Zulip message {msg.message_id}")
        await _reply(msg, client, NOT_CHECKED, "a sender was not looked up")
        return
    uploads = upload_links(msg.content)
    recorded = await sync_to_async(_record)(msg, ticket, email, bool(uploads))
    if recorded.refused:
        await _reply(msg, client, recorded.refused, "a message was refused")
    elif recorded.store_on is not None:
        if len(uploads) > MAX_UPLOADS:
            text = f"only a message's first {MAX_UPLOADS} files are added to the ticket; post the rest in another one."
            await _reply(msg, client, text, "files were left out")
        await _store_uploads(client, msg, *recorded.store_on, uploads[:MAX_UPLOADS])


async def apply_edit(event: Mapping[str, object]) -> None:
    """A message's new text onto its comment, while the comment's author may still write to the ticket
    (ticketing.chat_comments.apply_edit). Not passed on to the other chat backends."""
    if (edit := edited_message(event)) is not None:
        message_id, content = edit
        await sync_to_async(chat_comments.apply_edit)(TaskBackend.ZULIP, message_id, comment_text(content))


def follow_rename(stream: str, old: str, new: str) -> None:
    """Store a renamed topic's new name on the ticket the old name belongs to (find_ticket) while the new name still
    starts with that ticket's number (resolving, unresolving or retitling it in Zulip), so the portal's later messages
    go where Zulip now shows the ticket's. A rename to a name without the number, or naming another ticket, changes
    nothing: the portal keeps posting to the stored name."""
    ticket = find_ticket(stream, old)
    if ticket is None or ticket_number_from_topic(new) != ticket.ticket_number:
        return
    if Ticket.objects.filter(pk=ticket.pk).exclude(zulip_topic=new).update(zulip_topic=new):
        logger.info(f"Ticket {ticket.ticket_number}'s Zulip topic is now {new!r}")


async def apply_rename(event: Mapping[str, object]) -> None:
    """Follow a topic renamed in Zulip (follow_rename)."""
    if (rename := renamed_topic(event)) is not None:
        await sync_to_async(follow_rename)(*rename)


async def apply_delete(event: Mapping[str, object]) -> None:
    """Mark the deleted messages' comments deleted. Not passed on to the other chat backends."""
    if message_ids := deleted_message_ids(event):
        await sync_to_async(chat_comments.apply_delete)(TaskBackend.ZULIP, message_ids)


async def handle_event(client: ZulipClient, event: Mapping[str, object]) -> None:
    """One event from the queue: a message, an edit or topic rename, or a delete; any other (Zulip's heartbeat) needs
    nothing."""
    match event.get("type"):
        case "message":
            if (msg := parse_message_event(event)) is not None:
                await save_message(client, msg)
        case "update_message":
            await apply_edit(event)
            await apply_rename(event)
        case "delete_message":
            await apply_delete(event)
