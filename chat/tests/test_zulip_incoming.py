"""Messages in a ticket's Zulip topic become its comments: only the stream's own team's tickets, each message once,
under the same rate limit as the web and Discord; edits and deletes carry over and go no further. The runner's event
loop re-registers a lost queue and rides out Zulip being unreachable."""

import asyncio

import httpx
import pytest

from chat.tests.conftest import FakeZulip, message
from chat.zulip import runner
from chat.zulip.client import ZulipError
from chat.zulip.incoming import (
    IncomingMessage,
    apply_delete,
    apply_edit,
    deleted_message_ids,
    edited_message,
    parse_message_event,
    save_message,
)
from chat.zulip.naming import ticket_topic
from core.heartbeat import EVENTS_ZULIP
from core.models import QueuedTask
from ticketing.models import Ticket, TicketComment

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
        77, "team03", "T003-001: Web", 9, "member@example.org", "Member", "help"
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


def test_edits_and_deletes_are_read():
    assert edited_message({"type": "update_message", "message_id": 77, "content": "new"}) == (77, "new")
    # A topic move carries no content
    assert edited_message({"type": "update_message", "message_id": 77, "subject": "x"}) is None
    assert deleted_message_ids({"type": "delete_message", "message_id": 77}) == [77]
    assert deleted_message_ids({"type": "delete_message", "message_ids": [77, "x", 78]}) == [77, 78]


# ---------------------------------------------------------------------------------------------------------------
# Saving messages
# ---------------------------------------------------------------------------------------------------------------


async def test_reply_in_topic_becomes_comment(fake_zulip, ticket):
    await save_message(fake_zulip, message(topic=ticket_topic(ticket), content="hi"))

    comment = await TicketComment.objects.aget(ticket=ticket, comment_text="hi")
    assert comment.zulip_message_id == 77


async def test_other_teams_ticket_number_is_ignored(fake_zulip, team5_ticket):  # Review Focus 3
    await save_message(fake_zulip, message(stream="team03", topic=f"{team5_ticket.ticket_number}: x"))

    assert not await TicketComment.objects.aexists()


async def test_a_message_from_another_teams_stream_naming_this_ticket_is_ignored(fake_zulip, ticket, team5_ticket):
    await save_message(fake_zulip, message(stream="team05", topic=ticket_topic(ticket)))
    await save_message(fake_zulip, message(stream="general", topic=ticket_topic(ticket), message_id=78))

    assert not await TicketComment.objects.aexists()


async def test_a_topic_naming_no_ticket_is_ignored(fake_zulip, ticket):
    await save_message(fake_zulip, message(topic="lunch"))

    assert not await TicketComment.objects.aexists()


async def test_unknown_sender_recorded_by_name(fake_zulip, ticket):
    msg = message(topic=ticket_topic(ticket), sender_email="nobody@example.org", sender_name="Sender Name")

    await save_message(fake_zulip, msg)

    comment = await TicketComment.objects.aget()
    assert comment.author_id is None and comment.external_author == "Sender Name"
    assert comment.author_name == "Sender Name"


async def test_sender_matched_by_email(fake_zulip, ticket, django_user_model):
    user = await django_user_model.objects.acreate(username="team03", email="Team03@Example.org")

    await save_message(fake_zulip, message(topic=ticket_topic(ticket), sender_email="team03@example.org"))

    assert (await TicketComment.objects.aget()).author_id == user.id


@pytest.mark.parametrize("email", ["", "shared@example.org"])
async def test_a_blank_or_shared_email_names_no_user(fake_zulip, ticket, django_user_model, email):
    await django_user_model.objects.acreate(username="one", email=email)
    await django_user_model.objects.acreate(username="two", email=email)

    await save_message(fake_zulip, message(topic=ticket_topic(ticket), sender_email=email, sender_name="Member"))

    comment = await TicketComment.objects.aget()
    assert comment.author_id is None and comment.external_author == "Member"


async def test_redelivered_message_saved_once(fake_zulip, ticket):  # Review Focus 4
    msg = message(topic=ticket_topic(ticket), message_id=77)

    await save_message(fake_zulip, msg)
    await save_message(fake_zulip, msg)

    assert await TicketComment.objects.acount() == 1


async def test_rate_limit_blocks_and_replies(fake_zulip, ticket):
    for n in range(6):  # 5 per minute per ticket (CommentRateLimit.check_rate_limit)
        await save_message(fake_zulip, message(topic=ticket_topic(ticket), message_id=100 + n, content=f"m{n}"))

    assert await TicketComment.objects.acount() == 5
    [reply] = fake_zulip.sent_to("team03")
    assert "not added to the ticket" in reply


async def test_a_refusal_zulip_cant_take_is_logged(fake_zulip, ticket, caplog):
    for n in range(5):
        await save_message(fake_zulip, message(topic=ticket_topic(ticket), message_id=100 + n))
    fake_zulip.missing_streams.add("team03")

    await save_message(fake_zulip, message(topic=ticket_topic(ticket), message_id=200))

    assert "was refused" in caplog.text


async def test_comment_mirrors_to_discord_not_back_to_zulip(fake_zulip, ticket, zulip_configured):
    await QueuedTask.objects.all().adelete()

    await save_message(fake_zulip, message(topic=ticket_topic(ticket)))

    assert [t.backend async for t in QueuedTask.objects.all()] == ["discord"]


async def test_edit_and_delete_carry_over(fake_zulip, ticket):
    await save_message(fake_zulip, message(topic=ticket_topic(ticket), message_id=77, content="first"))
    await QueuedTask.objects.all().adelete()

    await apply_edit({"type": "update_message", "message_id": 77, "content": "second"})
    assert (await TicketComment.objects.aget()).comment_text == "second"

    await apply_delete({"type": "delete_message", "message_id": 77})
    assert (await TicketComment.objects.aget()).comment_text == "[Message deleted]"
    assert not await QueuedTask.objects.aexists()  # not mirrored


# ---------------------------------------------------------------------------------------------------------------
# The event loop
# ---------------------------------------------------------------------------------------------------------------


@pytest.fixture
def beats(monkeypatch) -> list[str]:
    recorded: list[str] = []
    monkeypatch.setattr(runner, "record", recorded.append)
    return recorded


async def test_lost_queue_reregisters(fake_zulip, ticket, beats):  # Review Focus 4
    fake_zulip.get_events_errors = [ZulipError("BAD_EVENT_QUEUE_ID")]
    fake_zulip.events = [[event(ticket_topic(ticket))]]

    with pytest.raises(asyncio.CancelledError):
        await runner.event_loop(fake_zulip)

    assert fake_zulip.registered == 2
    assert await TicketComment.objects.filter(zulip_message_id=77).aexists()
    assert beats == [EVENTS_ZULIP]


@pytest.mark.parametrize("error", [ZulipError("HTTP_502"), httpx.ConnectError("refused")])
async def test_an_unreachable_zulip_is_waited_out_without_beating(fake_zulip, beats, monkeypatch, error):
    monkeypatch.setattr(runner, "FIRST_BACKOFF_SECONDS", 0)
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
    assert polls_beaten == [0, 0, 0, 1]  # no beat until a poll is answered
    assert beats == [EVENTS_ZULIP]


async def test_a_failing_event_doesnt_stop_the_rest(fake_zulip, ticket, beats, monkeypatch):
    handle = runner.handle_event

    async def failing_first(client: FakeZulip, zulip_event: dict[str, object]) -> None:
        if zulip_event["id"] == 1:
            raise RuntimeError("bad event")
        await handle(client, zulip_event)

    monkeypatch.setattr(runner, "handle_event", failing_first)
    topic = ticket_topic(ticket)
    fake_zulip.events = [[event(topic, message_id=70, event_id=1), event(topic, message_id=71, event_id=2)]]

    with pytest.raises(asyncio.CancelledError):
        await runner.event_loop(fake_zulip)

    assert [c.zulip_message_id async for c in TicketComment.objects.all()] == [71]


async def test_the_runner_is_scoped_to_its_teams_ticket(fake_zulip, ticket, team5_ticket: Ticket, beats):
    """Review Focus 3 through the whole loop: team 3's stream, team 5's ticket number."""
    fake_zulip.events = [[event(f"{team5_ticket.ticket_number}: DNS")]]

    with pytest.raises(asyncio.CancelledError):
        await runner.event_loop(fake_zulip)

    assert not await TicketComment.objects.aexists()
