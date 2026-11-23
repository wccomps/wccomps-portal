"""Messages in a ticket's Zulip topic become its comments: only the stream's own team's tickets, only from a sender
whose real Zulip email (looked up, not the message's placeholder) is the portal account of the team's member or ticket
staff, each message once, under the same rate limit as the web and Discord; edits (while the author may still write)
and deletes carry over and go no further.
Uploaded files become the ticket's attachments, each message's once, credited to the sender's portal account. A topic
renamed in Zulip becomes the ticket's stored topic. The runner's event loop re-registers a lost queue and rides out
Zulip being unreachable."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from django.contrib.auth.models import User
from hypothesis import given
from hypothesis import strategies as st

from chat.tests.conftest import FakeZulip, message, portal_account
from chat.zulip import runner
from chat.zulip.client import LONG_POLL_READ_TIMEOUT, UPLOAD_PREFIX, ZulipError
from chat.zulip.incoming import (
    MAX_UPLOADS,
    NOT_CHECKED,
    NOT_LINKED,
    IncomingMessage,
    apply_delete,
    apply_edit,
    apply_rename,
    deleted_message_ids,
    edited_message,
    handle_event,
    parse_message_event,
    renamed_topic,
    save_message,
    upload_links,
)
from chat.zulip.naming import topic_name
from core.heartbeat import BUDGETS, EVENTS_ZULIP
from core.models import QueuedTask, UserGroups
from core.permission_constants import PERMISSION_MAP
from team.models import Team
from ticketing.models import CommentRateLimit, Ticket, TicketAttachment, TicketComment

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
PDF = b"%PDF-1.7\n" + b"\x00" * 16

# The async ORM runs on another thread's connection, which sees only committed rows
pytestmark = pytest.mark.django_db(transaction=True)


def event(topic: str, *, message_id: int = 77, event_id: int = 0, **fields: object) -> dict[str, object]:
    """A message event as Zulip's event queue delivers it: a member's message in team 3's stream."""
    return {
        "type": "message",
        "id": event_id,
        "message": {
            "id": message_id,
            "type": "stream",
            "display_recipient": "team03",
            "subject": topic,
            "sender_id": 9,
            "sender_email": "member@example.org",
            "sender_full_name": "Member",
            "content": "hi",
            **fields,
        },
    }


# ---------------------------------------------------------------------------------------------------------------
# Reading events
# ---------------------------------------------------------------------------------------------------------------


def test_a_stream_message_is_read():
    assert parse_message_event(event("T003-001: Web", content="help")) == IncomingMessage(
        77, "team03", "T003-001: Web", 9, "Member", "help"
    )


@pytest.mark.parametrize(
    "fields",
    [
        {"type": "private", "display_recipient": [{"id": 9}]},
        {"id": True},
        {"id": 2**63},
        {"sender_id": "9"},
        {"content": None},
    ],
)
def test_a_direct_or_unreadable_message_is_passed_over(fields):
    assert parse_message_event(event("T003-001: Web", **fields)) is None


def test_the_bots_own_messages_are_ignored(zulip_configured):
    assert parse_message_event(event("T003-001: Web", sender_email="Portal-Bot@Zulip.Example.org")) is None


async def test_a_system_bots_message_in_a_ticket_topic_saves_nothing_and_gets_no_reply(fake_zulip, ticket):
    """Resolving a topic makes Zulip's Notification Bot post in it; it is no one to tell NOT_LINKED."""
    notice = event(
        topic_name(ticket),
        sender_email="notification-bot@zulip.com",
        sender_full_name="Notification Bot",
        sender_realm_str="zulipinternal",
        content="@_**Member|9** has marked this topic as resolved.",
    )

    await handle_event(fake_zulip, notice)

    assert not await TicketComment.objects.aexists()
    assert fake_zulip.sent == []
    assert fake_zulip.user_lookups == []


def test_edits_and_deletes_are_read():
    assert edited_message({"type": "update_message", "message_id": 77, "content": "new"}) == (77, "new")
    # A topic move carries no content
    assert edited_message({"type": "update_message", "message_id": 77, "subject": "x"}) is None
    assert deleted_message_ids({"type": "delete_message", "message_id": 77}) == [77]
    assert deleted_message_ids({"type": "delete_message", "message_ids": [77, "x", 78]}) == [77, 78]


def rename(old: str, new: str, **fields: object) -> dict[str, object]:
    """An update_message event moving a topic's messages in team 3's stream, as resolving it in Zulip does."""
    return {
        "type": "update_message",
        "message_id": 77,
        "stream_name": "team03",
        "orig_subject": old,
        "subject": new,
        "propagate_mode": "change_all",
        **fields,
    }


def test_a_topic_rename_is_read():
    assert renamed_topic(rename("T003-001: Web", "✔ T003-001: Web")) == ("team03", "T003-001: Web", "✔ T003-001: Web")
    assert renamed_topic(rename("a", "b", propagate_mode="change_later")) == ("team03", "a", "b")


@pytest.mark.parametrize(
    "fields",
    [
        {"propagate_mode": "change_one"},  # one message moved: the topic keeps the rest
        {"new_stream_id": 5},  # moved to another stream
        {"subject": "T003-001: Web"},  # unchanged
        {"subject": "x" * 61},  # longer than a topic the ticket stores
        {"subject": "a\x00b"},
        {"subject": ""},
        {"orig_subject": None},
        {"type": "delete_message"},
    ],
)
def test_a_move_that_renames_no_topic_is_passed_over(fields):
    assert renamed_topic(rename("T003-001: Web", "✔ T003-001: Web", **fields)) is None


async def test_a_rename_in_zulip_keeping_the_number_becomes_the_tickets_topic(ticket):
    await apply_rename(rename(topic_name(ticket), f"✔ {topic_name(ticket)}"))
    assert (await Ticket.objects.aget(pk=ticket.pk)).zulip_topic == f"✔ {topic_name(ticket)}"

    await apply_rename(rename(f"✔ {topic_name(ticket)}", topic_name(ticket)))  # unresolved again
    assert (await Ticket.objects.aget(pk=ticket.pk)).zulip_topic == topic_name(ticket)

    await apply_rename(rename(topic_name(ticket), f"{ticket.ticket_number}: web is back"))  # retitled
    assert (await Ticket.objects.aget(pk=ticket.pk)).zulip_topic == f"{ticket.ticket_number}: web is back"


@pytest.mark.parametrize("new", ["lunch", "T003-002: DNS", "T005-001: DNS"])
async def test_a_rename_dropping_the_tickets_number_keeps_the_stored_topic(ticket, new):
    """Without the number (or naming another ticket) replies there no longer reach the ticket; the portal keeps
    posting to the stored name."""
    await Ticket.objects.filter(pk=ticket.pk).aupdate(zulip_topic=topic_name(ticket))

    await apply_rename(rename(topic_name(ticket), new))

    assert (await Ticket.objects.aget(pk=ticket.pk)).zulip_topic == topic_name(ticket)


async def test_a_topic_renamed_to_name_a_ticket_takes_nothing_over(ticket):
    await Ticket.objects.filter(pk=ticket.pk).aupdate(zulip_topic=topic_name(ticket))

    await apply_rename(rename("help", f"{ticket.ticket_number}: web"))

    assert (await Ticket.objects.aget(pk=ticket.pk)).zulip_topic == topic_name(ticket)


async def test_a_rename_in_another_teams_stream_changes_nothing(ticket, team5_ticket):
    await apply_rename(rename(topic_name(ticket), f"✔ {topic_name(ticket)}", stream_name="team05"))
    assert not await Ticket.objects.filter(zulip_topic=f"✔ {topic_name(ticket)}").aexists()


# ---------------------------------------------------------------------------------------------------------------
# Saving messages
# ---------------------------------------------------------------------------------------------------------------


async def test_reply_in_topic_becomes_comment(fake_zulip, ticket):
    await save_message(fake_zulip, message(topic=topic_name(ticket), content="hi"))

    comment = await TicketComment.objects.aget(ticket=ticket, comment_text="hi")
    assert comment.zulip_message_id == 77


async def test_other_teams_ticket_number_is_ignored(fake_zulip, team5_ticket):
    await save_message(fake_zulip, message(stream="team03", topic=f"{team5_ticket.ticket_number}: x"))

    assert not await TicketComment.objects.aexists()


async def test_a_message_from_another_teams_stream_naming_this_ticket_is_ignored(fake_zulip, ticket, team5_ticket):
    await save_message(fake_zulip, message(stream="team05", topic=topic_name(ticket)))
    await save_message(fake_zulip, message(stream="general", topic=topic_name(ticket), message_id=78))

    assert not await TicketComment.objects.aexists()


async def test_a_topic_naming_no_ticket_is_ignored(fake_zulip, ticket):
    await save_message(fake_zulip, message(topic="lunch"))

    assert not await TicketComment.objects.aexists()
    assert fake_zulip.sent == []
    assert fake_zulip.user_lookups == []  # no lookup for a message no ticket takes


async def test_sender_matched_by_email_writes_as_that_account(fake_zulip, ticket, django_user_model):
    member = await django_user_model.objects.aget(email="member@example.org")
    fake_zulip.user_emails[9] = "MEMBER@example.org"

    await save_message(fake_zulip, message(topic=topic_name(ticket)))

    assert (await TicketComment.objects.aget()).author_id == member.id
    assert fake_zulip.user_lookups == [9]


async def test_ticket_staff_may_write_to_any_teams_ticket(fake_zulip, ticket):
    staff = await portal_account("support", "support@example.org", [PERMISSION_MAP["ticketing_support"][0]])

    fake_zulip.user_emails[9] = "support@example.org"
    await save_message(fake_zulip, message(topic=topic_name(ticket)))

    assert (await TicketComment.objects.aget()).author_id == staff.id


async def _refused(fake_zulip: FakeZulip, ticket: Ticket, email: str) -> None:
    fake_zulip.uploads = {"/user_uploads/2/ab/a.txt": b"log"}
    fake_zulip.user_emails[9] = email
    content = "hello [a.txt](/user_uploads/2/ab/a.txt)"
    await save_message(fake_zulip, message(topic=topic_name(ticket), content=content))

    assert not await TicketComment.objects.aexists()
    assert not await TicketAttachment.objects.aexists()
    assert not await CommentRateLimit.objects.aexists()
    assert fake_zulip.sent_to("team03") == [f"Member: {NOT_LINKED}"]


@pytest.mark.parametrize("email", ["nobody@example.org", ""])
async def test_a_sender_with_no_portal_account_is_told_and_nothing_is_saved(fake_zulip, ticket, email):
    await _refused(fake_zulip, ticket, email)


async def test_a_sender_whose_address_zulip_hides_is_refused(fake_zulip, ticket):
    """Zulip gives a placeholder (user<id>@<host>) for a sender whose email visibility leaves the bot out."""
    await _refused(fake_zulip, ticket, "user9@zulip.example.org")


@pytest.mark.parametrize("error", [ZulipError("BAD_REQUEST", "No such user"), httpx.ConnectError("refused")])
async def test_a_sender_zulip_cant_be_asked_about_is_told_to_send_again(fake_zulip, ticket, caplog, error):
    """Not NOT_LINKED: no one was found not to match; nothing is saved and the failure is logged."""
    fake_zulip.user_lookup_error = error
    fake_zulip.uploads = {"/user_uploads/2/ab/a.txt": b"log"}

    await save_message(fake_zulip, message(topic=topic_name(ticket), content="hi [a.txt](/user_uploads/2/ab/a.txt)"))

    assert not await TicketComment.objects.aexists()
    assert not await TicketAttachment.objects.aexists()
    assert not await CommentRateLimit.objects.aexists()
    assert fake_zulip.fetched == []
    assert fake_zulip.sent_to("team03") == [f"Member: {NOT_CHECKED}"]
    assert "Could not look up the sender of Zulip message 77" in caplog.text


async def test_an_email_shared_by_two_accounts_names_no_one(fake_zulip, ticket, django_user_model):
    await django_user_model.objects.acreate(username="other", email="Member@Example.org")

    await _refused(fake_zulip, ticket, "member@example.org")


async def test_another_teams_member_is_refused(fake_zulip, ticket):
    team5 = await Team.objects.aget(team_number=5)
    await portal_account("member05", "five@example.org", [team5.authentik_group])

    await _refused(fake_zulip, ticket, "five@example.org")


async def test_a_disabled_account_is_refused(fake_zulip, ticket):
    team = await Team.objects.aget(team_number=3)
    await portal_account("off03", "off@example.org", [team.authentik_group], active=False)

    await _refused(fake_zulip, ticket, "off@example.org")


async def test_redelivered_message_saved_once(fake_zulip, ticket):
    msg = message(topic=topic_name(ticket), message_id=77)

    await save_message(fake_zulip, msg)
    await save_message(fake_zulip, msg)

    assert await TicketComment.objects.acount() == 1
    assert await CommentRateLimit.objects.acount() == 1


async def test_rate_limit_blocks_and_replies(fake_zulip, ticket):
    for n in range(6):  # 5 per minute per ticket (CommentRateLimit.TICKET_LIMIT)
        await save_message(fake_zulip, message(topic=topic_name(ticket), message_id=100 + n, content=f"m{n}"))

    assert await TicketComment.objects.acount() == 5
    [reply] = fake_zulip.sent_to("team03")
    assert "not added to the ticket" in reply


async def test_the_senders_portal_account_has_one_budget_on_every_surface(fake_zulip, ticket):
    """Its web and Discord posts count against its Zulip ones: a team's shared account gets one budget."""
    member = await User.objects.aget(username="member03")
    for _ in range(CommentRateLimit.ACTOR_LIMIT):
        await CommentRateLimit.objects.acreate(ticket=None, actor_key=CommentRateLimit.user_key(member))

    await save_message(fake_zulip, message(topic=topic_name(ticket), content="one more"))

    assert not await TicketComment.objects.aexists()
    [reply] = fake_zulip.sent_to("team03")
    assert "User rate limit exceeded" in reply


async def test_a_message_past_the_comment_limit_is_refused_whole(fake_zulip, ticket):
    """Refused, not cut, as the web form refuses it: the comment is never part of what was said."""
    fake_zulip.uploads = {"/user_uploads/2/ab/a.txt": b"log"}
    content = "x" * TicketComment.MAX_LENGTH + "y [a.txt](/user_uploads/2/ab/a.txt)"

    await save_message(fake_zulip, message(topic=topic_name(ticket), content=content))

    assert not await TicketComment.objects.aexists()
    assert not await TicketAttachment.objects.aexists()
    assert not await CommentRateLimit.objects.aexists()
    [reply] = fake_zulip.sent_to("team03")
    assert f"Comment too long ({TicketComment.MAX_LENGTH + 1}/{TicketComment.MAX_LENGTH} characters)" in reply


async def test_a_message_at_the_comment_limit_is_saved(fake_zulip, ticket):
    await save_message(fake_zulip, message(topic=topic_name(ticket), content="x" * TicketComment.MAX_LENGTH))

    assert len((await TicketComment.objects.aget()).comment_text) == TicketComment.MAX_LENGTH


async def test_an_edit_past_the_comment_limit_changes_nothing(fake_zulip, ticket):
    await save_message(fake_zulip, message(topic=topic_name(ticket), message_id=77, content="first"))

    await apply_edit({"type": "update_message", "message_id": 77, "content": "x" * (TicketComment.MAX_LENGTH + 1)})

    assert (await TicketComment.objects.aget()).comment_text == "first"


async def test_a_refusal_zulip_cant_take_is_logged(fake_zulip, ticket, caplog):
    for n in range(5):
        await save_message(fake_zulip, message(topic=topic_name(ticket), message_id=100 + n))
    fake_zulip.missing_streams.add("team03")

    await save_message(fake_zulip, message(topic=topic_name(ticket), message_id=200))

    assert "was refused" in caplog.text


async def test_comment_mirrors_to_discord_not_back_to_zulip(fake_zulip, ticket, zulip_configured):
    await QueuedTask.objects.all().adelete()

    await save_message(fake_zulip, message(topic=topic_name(ticket)))

    assert [t.backend async for t in QueuedTask.objects.all()] == ["discord"]


async def test_edit_and_delete_carry_over(fake_zulip, ticket):
    await save_message(fake_zulip, message(topic=topic_name(ticket), message_id=77, content="first"))
    await QueuedTask.objects.all().adelete()

    await apply_edit({"type": "update_message", "message_id": 77, "content": "second\x00"})
    assert (await TicketComment.objects.aget()).comment_text == "second"

    await apply_delete({"type": "delete_message", "message_id": 77})
    assert (await TicketComment.objects.aget()).comment_text == "[Message deleted]"
    assert not await QueuedTask.objects.aexists()  # not mirrored


async def test_an_edit_by_an_author_since_disabled_changes_nothing(fake_zulip, ticket):
    await save_message(fake_zulip, message(topic=topic_name(ticket), message_id=77, content="first"))
    await UserGroups.objects.filter(user__username="member03").aupdate(account_active=False)

    await apply_edit({"type": "update_message", "message_id": 77, "content": "second"})

    assert (await TicketComment.objects.aget()).comment_text == "first"


# ---------------------------------------------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------------------------------------------


def test_upload_links_finds_the_sites_uploads_only():
    content = (
        "logs [a.txt](/user_uploads/2/ab/a.txt) and [site](https://example.org/x.png) "
        "[b](/user_uploads/2/../../api/v1/users) [again](/user_uploads/2/ab/a.txt) [](/user_uploads/2/cd/c.png) "
        "[c](/user_uploads/%2e%2e/api/v1/messages/1) [d](/user_uploads/2/ab\\..\\..\\api) [e](/user_uploads/2/./x.png)"
    )

    assert upload_links(content) == [("a.txt", "/user_uploads/2/ab/a.txt"), ("", "/user_uploads/2/cd/c.png")]


def test_upload_links_counts_a_file_once_whatever_its_fragment_or_query():
    content = "".join(f"[f](/user_uploads/2/ab/f.bin#{n}) [f](/user_uploads/2/ab/f.bin?v={n})" for n in range(50))

    assert upload_links(content) == [("f", "/user_uploads/2/ab/f.bin")]


_LINK_PARTS = st.sampled_from(
    ["[", "]", "(", ")", UPLOAD_PREFIX, "..", "/", " ", "\n", "#", "?", "a.png", "https://x/"]
)


@given(st.lists(st.one_of(st.text(), _LINK_PARTS)).map("".join))
def test_upload_links_reads_any_text(content):
    paths = [path for _, path in upload_links(content)]
    assert len(paths) == len(set(paths))
    for path in paths:
        assert path.startswith(UPLOAD_PREFIX) and ".." not in path.split("/")
        assert "%" not in path and "\\" not in path
        assert "#" not in path and "?" not in path


async def test_uploads_become_attachments_with_clean_names_and_sniffed_types(fake_zulip, ticket):
    fake_zulip.uploads = {"/user_uploads/2/ab/shot.bin": PNG, "/user_uploads/2/cd/report.pdf": PDF}
    content = "[shot.bin](/user_uploads/2/ab/shot.bin)\n[re\u202eport.pdf](/user_uploads/2/cd/report.pdf)"

    # A display name anyone can set: the files are credited to the matched portal account
    await save_message(fake_zulip, message(topic=topic_name(ticket), content=content, sender_name="Support"))

    stored = [(a.filename, a.mime_type, a.uploaded_by, a.ticket_id) async for a in TicketAttachment.objects.all()]
    assert stored == [
        ("shot.bin", "image/png", "member03", ticket.id),
        ("report.pdf", "application/pdf", "member03", ticket.id),
    ]
    assert fake_zulip.reactions == [(77, "paperclip")]
    # Only files, as a Discord message with only attachments: no comment
    assert not await TicketComment.objects.aexists()


async def test_text_beside_an_upload_is_the_comment(fake_zulip, ticket):
    fake_zulip.uploads = {"/user_uploads/2/ab/a.txt": b"log"}

    await save_message(fake_zulip, message(topic=topic_name(ticket), content="see [a.txt](/user_uploads/2/ab/a.txt)"))

    assert (await TicketComment.objects.aget()).comment_text == "see"
    assert await TicketAttachment.objects.acount() == 1
    assert await CommentRateLimit.objects.acount() == 1  # one message, one count


async def test_a_message_stores_at_most_discords_attachment_cap(fake_zulip, ticket):
    paths = [f"/user_uploads/2/ab/{n}.txt" for n in range(MAX_UPLOADS + 2)]
    fake_zulip.uploads = dict.fromkeys(paths, b"log")

    await save_message(fake_zulip, message(topic=topic_name(ticket), content=" ".join(f"[f]({p})" for p in paths)))

    assert fake_zulip.fetched == paths[:MAX_UPLOADS]
    assert await TicketAttachment.objects.acount() == MAX_UPLOADS
    assert fake_zulip.sent_to("team03") == [
        f"Member: only a message's first {MAX_UPLOADS} files are added to the ticket; post the rest in another one."
    ]


async def test_an_oversize_upload_is_not_stored_and_replied_to(fake_zulip, ticket):
    fake_zulip.uploads = {"/user_uploads/2/ab/big.iso": b"x" * (TicketAttachment.MAX_SIZE + 5)}

    await save_message(fake_zulip, message(topic=topic_name(ticket), content="[big.iso](/user_uploads/2/ab/big.iso)"))

    assert not await TicketAttachment.objects.aexists()
    assert fake_zulip.sent_to("team03") == ["Member: File too large (max 10MB). Please use a file sharing service."]
    assert fake_zulip.reactions == []


async def test_an_upload_at_the_size_limit_is_stored(fake_zulip, ticket):
    fake_zulip.uploads = {"/user_uploads/2/ab/max.bin": b"x" * TicketAttachment.MAX_SIZE}

    await save_message(fake_zulip, message(topic=topic_name(ticket), content="[max.bin](/user_uploads/2/ab/max.bin)"))

    assert await TicketAttachment.objects.acount() == 1


async def test_an_upload_that_cant_be_fetched_is_replied_to(fake_zulip, ticket, monkeypatch):
    async def gone(path: str, limit: int) -> bytes:
        raise ZulipError("HTTP_404")

    monkeypatch.setattr(fake_zulip, "fetch_upload", gone)

    await save_message(fake_zulip, message(topic=topic_name(ticket), content="[a](/user_uploads/2/ab/a.txt)"))

    assert not await TicketAttachment.objects.aexists()
    assert fake_zulip.sent_to("team03") == ["Member: Failed to store an attachment. Please try again."]


async def test_a_link_outside_user_uploads_is_ignored(fake_zulip, ticket):
    content = "[x.png](https://example.org/user_uploads/x.png) [y](/static/y.png)"

    await save_message(fake_zulip, message(topic=topic_name(ticket), content=content))

    assert not await TicketAttachment.objects.aexists()
    assert fake_zulip.reactions == []
    assert (await TicketComment.objects.aget()).comment_text == content


async def test_a_redelivered_upload_is_stored_once(fake_zulip, ticket):
    fake_zulip.uploads = {"/user_uploads/2/ab/a.txt": b"log"}
    msg = message(topic=topic_name(ticket), content="[a.txt](/user_uploads/2/ab/a.txt)", message_id=77)

    await save_message(fake_zulip, msg)
    await save_message(fake_zulip, msg)

    assert [a.zulip_message_id async for a in TicketAttachment.objects.all()] == [77]
    assert fake_zulip.reactions == [(77, "paperclip")]


@pytest.mark.parametrize("status", [Ticket.STATUS_RESOLVED, Ticket.STATUS_CANCELLED])
async def test_a_closed_ticket_takes_no_uploads(fake_zulip, ticket, status):
    await Ticket.objects.filter(pk=ticket.pk).aupdate(status=status)
    fake_zulip.uploads = {"/user_uploads/2/ab/a.txt": b"log", "/user_uploads/2/ab/b.txt": b"log"}
    content = "[a.txt](/user_uploads/2/ab/a.txt) [b.txt](/user_uploads/2/ab/b.txt)"

    await save_message(fake_zulip, message(topic=topic_name(ticket), content=content))

    assert not await TicketAttachment.objects.aexists()
    # One reply for the message, not one per file
    assert fake_zulip.sent_to("team03") == [
        f"Member: Ticket is {status}; files can't be added to it. Files from here on were not added."
    ]
    assert fake_zulip.reactions == []


async def test_uploads_past_the_teams_byte_cap_are_refused(fake_zulip, ticket, monkeypatch):
    monkeypatch.setattr(TicketAttachment, "MAX_TEAM_BYTES", 5)
    await TicketAttachment.objects.acreate(
        ticket=ticket, file_data=b"abc", filename="old.txt", mime_type="text/plain", uploaded_by="x"
    )
    fake_zulip.uploads = {"/user_uploads/2/ab/a.txt": b"log"}

    await save_message(fake_zulip, message(topic=topic_name(ticket), content="[a.txt](/user_uploads/2/ab/a.txt)"))

    assert await TicketAttachment.objects.acount() == 1
    [reply] = fake_zulip.sent_to("team03")
    assert "total limit" in reply


# ---------------------------------------------------------------------------------------------------------------
# The event loop
# ---------------------------------------------------------------------------------------------------------------


@pytest.fixture
def beats(monkeypatch) -> list[str]:
    recorded: list[str] = []
    monkeypatch.setattr(runner, "record", recorded.append)
    return recorded


@pytest.fixture
def waits(monkeypatch) -> list[float]:
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(runner, "asyncio", SimpleNamespace(sleep=sleep))
    return slept


async def test_lost_queue_reregisters_at_once(fake_zulip, ticket, beats, waits):
    fake_zulip.events = [[], ZulipError("BAD_EVENT_QUEUE_ID"), [event(topic_name(ticket))]]

    with pytest.raises(asyncio.CancelledError):
        await runner.event_loop(fake_zulip)

    assert fake_zulip.registered == 2
    assert waits == []
    assert await TicketComment.objects.filter(zulip_message_id=77).aexists()
    assert beats == [EVENTS_ZULIP] * 2


async def test_a_queue_lost_before_its_first_poll_is_answered_waits_first(fake_zulip, beats, waits):
    lost = ZulipError("BAD_EVENT_QUEUE_ID")
    fake_zulip.events = [[], lost, lost, lost, []]

    with pytest.raises(asyncio.CancelledError):
        await runner.event_loop(fake_zulip)

    # The answered queue's loss re-registers at once; each new queue lost before an answer waits first
    assert fake_zulip.registered == 4
    assert waits == [runner.FIRST_BACKOFF_SECONDS, 2 * runner.FIRST_BACKOFF_SECONDS]
    assert beats == [EVENTS_ZULIP] * 4


@pytest.mark.parametrize("error", [ZulipError("HTTP_502"), httpx.ConnectError("refused")])
async def test_an_unreachable_zulip_is_waited_out_beating_after_each_wait(fake_zulip, beats, waits, monkeypatch, error):
    fake_zulip.get_events_errors = [error, error]
    fake_zulip.events = [[]]
    polls_beaten: list[int] = []
    get_events = fake_zulip.get_events

    async def counting(queue_id: str, last_event_id: int) -> list[dict[str, object]]:
        polls_beaten.append(len(beats))
        return await get_events(queue_id, last_event_id)

    monkeypatch.setattr(fake_zulip, "get_events", counting)

    with pytest.raises(asyncio.CancelledError):
        await runner.event_loop(fake_zulip)

    assert fake_zulip.registered == 1  # the same queue, polled again
    assert waits == [runner.FIRST_BACKOFF_SECONDS, 2 * runner.FIRST_BACKOFF_SECONDS]
    # A beat after each failed poll's wait and after the answered one: an outage alone never goes stale
    assert polls_beaten == [0, 1, 2, 3]
    assert beats == [EVENTS_ZULIP] * 3


def test_a_backed_off_loop_beats_well_within_its_budget():
    """The longest gap between beats while Zulip is down: a poll that times out, then the longest wait."""
    assert BUDGETS[EVENTS_ZULIP] >= 2 * (LONG_POLL_READ_TIMEOUT + runner.MAX_BACKOFF_SECONDS)


async def test_a_failing_event_doesnt_stop_the_rest(fake_zulip, ticket, beats, monkeypatch):
    handle = runner.handle_event

    async def failing_first(client: FakeZulip, zulip_event: dict[str, object]) -> None:
        if zulip_event["id"] == 1:
            raise RuntimeError("bad event")
        await handle(client, zulip_event)

    monkeypatch.setattr(runner, "handle_event", failing_first)
    topic = topic_name(ticket)
    fake_zulip.events = [[event(topic, message_id=70, event_id=1), event(topic, message_id=71, event_id=2)]]

    with pytest.raises(asyncio.CancelledError):
        await runner.event_loop(fake_zulip)

    assert [c.zulip_message_id async for c in TicketComment.objects.all()] == [71]


async def test_the_runner_is_scoped_to_its_teams_ticket(fake_zulip, ticket, team5_ticket: Ticket, beats):
    """Through the whole loop: team 3's stream, team 5's ticket number."""
    fake_zulip.events = [[event(f"{team5_ticket.ticket_number}: DNS")]]

    with pytest.raises(asyncio.CancelledError):
        await runner.event_loop(fake_zulip)

    assert not await TicketComment.objects.aexists()
