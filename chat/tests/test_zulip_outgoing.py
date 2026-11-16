"""The chat notices ZulipQueueProcessor delivers: a ticket's topic in its team's stream, broadcasts that skip a missing
stream and resume after a failure, and the ops and status messages."""

from dataclasses import asdict
from datetime import UTC, datetime

import httpx
import pytest
from asgiref.sync import sync_to_async
from django.urls import reverse

from chat.tests.conftest import FakeZulip
from chat.zulip.client import ZulipError
from chat.zulip.markup import to_zulip
from chat.zulip.naming import team_for_stream, team_stream, ticket_number_from_topic, ticket_topic
from chat.zulip.outgoing import MISSING_STREAMS, ZulipQueueProcessor
from core.models import CompetitionConfig, QueuedTask
from core.queued_tasks import (
    BroadcastMessage,
    CompetitionStatusChanged,
    LogToChannel,
    PermanentTaskError,
    PostComment,
    PostTicketUpdate,
    SyncRoles,
    TaskPayload,
    TicketCreated,
)
from ticketing.models import Ticket, TicketComment

# The async ORM runs on another thread's connection, which sees only committed rows
pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def processor(fake_zulip: FakeZulip) -> ZulipQueueProcessor:
    return ZulipQueueProcessor(fake_zulip)


async def claimed(payload: TaskPayload) -> QueuedTask:
    """payload's row as the queue claims it."""
    return await QueuedTask.objects.acreate(
        task_type=payload.task_type, backend="zulip", payload=asdict(payload), status="processing"
    )


async def run(processor: ZulipQueueProcessor, payload: TaskPayload, task: QueuedTask | None = None) -> object:
    """Run payload's handler as the queue does; given task, a retry of it, with the progress its row saved."""
    task = await QueuedTask.objects.aget(pk=task.pk) if task else await claimed(payload)
    return await processor.dispatch(payload, task)


def test_ticket_topic_long_title_fits_and_maps_back(ticket: Ticket) -> None:  # Review Focus 6
    ticket.title = "x" * 200 + " **bold** `code`"
    assert len(ticket_topic(ticket)) <= 60
    assert ticket_number_from_topic(ticket_topic(ticket)) == ticket.ticket_number


def test_ticket_topic_is_one_line(ticket: Ticket) -> None:
    ticket.title = "Web\nserver   down"
    assert ticket_topic(ticket) == "T003-001: Web server down"


def test_team_stream_maps_back_to_its_team() -> None:
    assert team_stream(7) == "team07"
    assert team_for_stream("team07") == 7
    assert team_for_stream("tickets") is None


def test_discord_timestamp_becomes_zulip_time() -> None:
    assert to_zulip("ends at 5 PM (<t:1794772800:t>).") == "ends at 5 PM (<time:2026-11-15T20:00:00+00:00>)."
    assert to_zulip("<t:1794772800>") == "<time:2026-11-15T20:00:00+00:00>"


def test_out_of_range_timestamp_left_as_written() -> None:
    assert to_zulip("<t:99999999999999999999>") == "<t:99999999999999999999>"


async def test_broadcast_all_teams_skips_missing_stream(processor, fake_zulip, make_teams):  # Review Focus 5
    await make_teams(active=[1, 2, 3])
    fake_zulip.missing_streams = {"team02"}
    result = await run(processor, BroadcastMessage(target="all-teams", message="m", sender="gold"))
    assert [s for s in fake_zulip.sent_streams if s != "ops"] == ["team01", "team03"]
    assert fake_zulip.sent_to("ops") == [MISSING_STREAMS.format(sender="gold", target="all-teams", teams="02")]
    assert result == {"delivered": [1, 3], "missing": [2]}


async def test_broadcast_retry_skips_teams_reached(processor, fake_zulip, make_teams):  # Review Focus 5
    await make_teams(active=[1, 2, 3])
    fake_zulip.fail_once_on = "team03"
    payload = BroadcastMessage(target="all-teams", message="m", sender="gold")
    task = await claimed(payload)
    with pytest.raises(ZulipError):
        await run(processor, payload, task)
    await run(processor, payload, task)
    assert fake_zulip.sent_streams == ["team01", "team02", "team03"]


async def test_broadcast_retries_a_lost_connection(processor, fake_zulip, make_teams, monkeypatch):
    await make_teams(active=[1, 2])
    send = fake_zulip.send_stream_message
    failed: list[str] = []

    async def drop_once(stream: str, topic: str, content: str) -> int:
        if stream == "team02" and not failed:
            failed.append(stream)
            raise httpx.ConnectError("connection refused")
        return await send(stream, topic, content)

    monkeypatch.setattr(fake_zulip, "send_stream_message", drop_once)
    payload = BroadcastMessage(target="all-teams", message="m", sender="gold")
    task = await claimed(payload)
    with pytest.raises(httpx.ConnectError):
        await run(processor, payload, task)
    assert await run(processor, payload, task) == {"delivered": [1, 2], "missing": []}
    assert fake_zulip.sent_streams == ["team01", "team02"]


async def test_ranged_broadcast_never_posts_to_shared_stream(processor, fake_zulip, make_teams):
    await make_teams(active=[1, 2, 3, 4, 5, 6])
    await run(processor, BroadcastMessage(target="3-5", message="m", sender="gold"))
    assert fake_zulip.sent_streams == ["team03", "team04", "team05"]
    assert fake_zulip.sent[0][2] == "**Announcement from gold:**\n\nm"


async def test_broadcast_to_announcements_stream(processor, fake_zulip, make_teams):
    await make_teams(active=[1, 2])
    await run(processor, BroadcastMessage(target="announcements", message="hello @**all**", sender="gold"))
    assert fake_zulip.sent == [("announcements", "announcements", "hello @_**all**")]


async def test_broadcast_stops_once_its_task_was_failed(processor, fake_zulip, make_teams):
    await make_teams(active=[1, 2])
    payload = BroadcastMessage(target="all-teams", message="m", sender="gold")
    task = await claimed(payload)
    await QueuedTask.objects.filter(pk=task.pk).aupdate(status="failed")
    with pytest.raises(PermanentTaskError):
        await run(processor, payload, task)
    assert fake_zulip.sent == []


async def test_ticket_created_posts_topic_and_staff_alert(processor, fake_zulip, ticket, settings):
    settings.ZULIP_TEAM_MENTION, settings.ZULIP_STAFF_MENTION = "Team {number:02d}", "Support"
    await run(processor, TicketCreated(ticket_id=ticket.id))
    (stream, topic, content), (staff_stream, _, alert) = fake_zulip.sent
    assert (stream, topic) == ("team03", ticket_topic(ticket))
    assert content.splitlines()[0] == "@*Team 03* @*Support* New ticket T003-001: Web server down"
    assert staff_stream == "tickets"
    assert f"{settings.BASE_URL}{reverse('ticket_detail', args=[ticket.ticket_number])}" in alert


async def test_ticket_created_without_mentions(processor, fake_zulip, ticket):
    await run(processor, TicketCreated(ticket_id=ticket.id))
    assert fake_zulip.sent[0][2].splitlines()[0] == "New ticket T003-001: Web server down"


async def test_ticket_created_retry_skips_the_post_already_made(processor, fake_zulip, ticket):
    fake_zulip.fail_once_on = "tickets"
    payload = TicketCreated(ticket_id=ticket.id)
    task = await claimed(payload)
    with pytest.raises(ZulipError):
        await run(processor, payload, task)
    await run(processor, payload, task)
    assert fake_zulip.sent_streams == ["team03", "tickets"]


async def test_missing_team_stream_fails_ticket_post_for_good(processor, fake_zulip, ticket_with_comment):
    ticket, comment = ticket_with_comment
    fake_zulip.missing_streams = {"team03"}
    with pytest.raises(PermanentTaskError, match="team03"):
        await run(processor, PostComment(ticket_id=ticket.id, comment_id=comment.id))


async def test_comment_posts_to_ticket_topic_with_mentions_silenced(processor, fake_zulip, ticket_with_comment):
    ticket, comment = ticket_with_comment
    await TicketComment.objects.filter(pk=comment.pk).aupdate(comment_text="ping @**all** and @*Support*")
    await run(processor, PostComment(ticket_id=ticket.id, comment_id=comment.id))
    assert fake_zulip.sent == [("team03", ticket_topic(ticket), "**Unknown**\nping @_**all** and @_*Support*")]


async def test_long_comment_sent_in_parts_and_resumed(processor, fake_zulip, ticket_with_comment):
    ticket, comment = ticket_with_comment
    await TicketComment.objects.filter(pk=comment.pk).aupdate(comment_text="\n".join(["x" * 999] * 25))
    payload = PostComment(ticket_id=ticket.id, comment_id=comment.id)
    task = await claimed(payload)
    send = fake_zulip.send_stream_message
    calls: list[int] = []

    async def fail_second(stream: str, topic: str, content: str) -> int:
        calls.append(1)
        if len(calls) == 2:
            raise ZulipError("HTTP_502")
        return await send(stream, topic, content)

    fake_zulip.send_stream_message = fail_second
    with pytest.raises(ZulipError):
        await run(processor, payload, task)
    await run(processor, payload, task)
    parts = fake_zulip.sent_to("team03")
    assert len(parts) == 3 and all(len(part) <= 10000 for part in parts)
    assert "\n".join(parts).count("x" * 999) == 25


async def test_ticket_update_wording(processor, fake_zulip, ticket):
    await run(processor, PostTicketUpdate(ticket_id=ticket.id, action="claimed", actor="support1"))
    await run(
        processor,
        PostTicketUpdate(
            ticket_id=ticket.id, action="resolved", actor="support1", points_charged=5, resolution_notes="Fixed"
        ),
    )
    await run(processor, PostTicketUpdate(ticket_id=ticket.id, action="reopened", actor="team", reason="Still down"))
    assert fake_zulip.sent_to("team03") == [
        "Ticket claimed by **support1**",
        "**Ticket Resolved**\nResolved By: support1\nPoints Charged: 5\nResolution Notes: Fixed",
        "Ticket reopened by **team**\nReason: Still down",
    ]
    assert {topic for _, topic, _ in fake_zulip.sent} == {ticket_topic(ticket)}


async def test_log_to_channel_posts_to_ops_topic(processor, fake_zulip):
    await run(processor, LogToChannel(message="hello"))
    assert fake_zulip.sent == [("ops", "ops", "hello")]


async def test_empty_log_fails_for_good(processor):
    with pytest.raises(PermanentTaskError):
        await run(processor, LogToChannel(message=""))


async def test_competition_status_shows_the_status_facts(processor, fake_zulip, settings):
    start = datetime(2026, 11, 15, 17, tzinfo=UTC)
    await sync_to_async(CompetitionConfig.get_config)()
    await CompetitionConfig.objects.filter(pk=1).aupdate(
        applications_enabled=True, competition_start_time=start, controlled_applications=["netbird"]
    )
    await run(processor, CompetitionStatusChanged())
    ((stream, topic, content),) = fake_zulip.sent
    assert (stream, topic) == ("ops", "competition status")
    assert content.splitlines() == [
        "**Competition Status: RUNNING**",
        "Scheduled Start: <time:2026-11-15T17:00:00+00:00>",
        "Controlled Applications: netbird",
        f"Team Accounts: Enabled ({settings.MAX_TEAMS} teams)",
    ]


async def test_discord_only_payload_fails_permanently(processor):
    with pytest.raises(PermanentTaskError):
        await run(processor, SyncRoles(requested_by="a", dry_run=True))
