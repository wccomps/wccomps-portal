"""A retried task does only the steps its failed attempt didn't."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from bot.tests.discord_fakes import bot_with, recording_thread, ticket_message
from core.models import DiscordTask
from team.models import Team
from ticketing.models import Ticket, TicketCategory

pytestmark = [pytest.mark.asyncio, pytest.mark.django_db(transaction=True)]

LONG = "\n".join(f"line {n:04d} " + "x" * 80 for n in range(60))  # about three Discord messages


def flaky_send(sent: list[str], fail_on: int) -> AsyncMock:
    """A send that records each text and fails once, on call number fail_on."""
    calls = {"n": 0}

    async def send(content: str | None = None, **kwargs: Any) -> MagicMock:
        calls["n"] += 1
        if calls["n"] == fail_on:
            raise discord.HTTPException(MagicMock(status=500), "Internal Server Error")
        sent.append(content or kwargs["embed"].title)
        return MagicMock(id=1000 + calls["n"], pin=AsyncMock())

    return AsyncMock(side_effect=send)


async def run_twice(processor: Any, task: DiscordTask) -> None:
    await processor._process_task(task)
    await task.arefresh_from_db()
    assert task.status == "pending"
    await DiscordTask.objects.filter(pk=task.pk).aupdate(next_retry_at=None)
    await processor._process_task(task)
    await task.arefresh_from_db()
    assert task.status == "completed"


@pytest.fixture
async def ticket(db: Any) -> Ticket:
    team = await Team.objects.acreate(team_number=24, team_name="Team 24", discord_category_id=2400)
    category = await TicketCategory.objects.acreate(pk=92, display_name="Other", points=0, sort_order=92)
    return await Ticket.objects.acreate(
        ticket_number="T024-001", team=team, category=category, title="Help", status="open", discord_thread_id=2499
    )


async def test_a_long_comment_retried_after_its_second_part_failed_sends_each_part_once(ticket: Ticket) -> None:
    from bot.discord_queue import DiscordQueueProcessor

    thread = recording_thread(2499, failing_sends={2}, pinned_text=ticket_message(ticket.ticket_number))
    comment = await ticket.comments.acreate(comment_text=LONG)
    task = await DiscordTask.objects.acreate(
        task_type="post_comment", ticket=ticket, payload={"ticket_id": ticket.id, "comment_id": comment.id}
    )

    with patch("bot.discord_queue.competition_guild", return_value=MagicMock(spec=discord.Guild)):
        await run_twice(DiscordQueueProcessor(bot_with(thread)), task)

    assert len(thread.texts) == len(set(thread.texts)) == 3


async def test_a_long_reopen_reason_retried_sends_each_part_once(ticket: Ticket) -> None:
    from bot.discord_queue import DiscordQueueProcessor

    thread = recording_thread(2499, failing_sends={2}, pinned_text=ticket_message(ticket.ticket_number))
    task = await DiscordTask.objects.acreate(
        task_type="post_ticket_update",
        ticket=ticket,
        payload={"ticket_id": ticket.id, "action": "reopened", "actor": "ops", "reason": LONG},
    )

    with patch("bot.discord_queue.competition_guild", return_value=MagicMock(spec=discord.Guild)):
        await run_twice(DiscordQueueProcessor(bot_with(thread)), task)

    assert len(thread.texts) == len(set(thread.texts)) == 3


async def test_a_team_broadcast_retried_reaches_only_the_teams_it_missed() -> None:
    """Team 42's send failed: the retry sends only to it, and the counts cover both attempts."""
    from bot.discord_queue import DiscordQueueProcessor

    channels = {}
    for number in (41, 42, 43):
        team = await Team.objects.acreate(team_number=number, team_name=f"Team {number}", discord_category_id=number)
        chat = MagicMock(spec=discord.TextChannel)
        chat.name = f"team{number}-chat"
        chat.send = AsyncMock(
            side_effect=[discord.HTTPException(MagicMock(status=500), "Internal Server Error"), None]
            if number == 42
            else None
        )
        channels[team.discord_category_id] = MagicMock(spec=discord.CategoryChannel, channels=[chat])
    guild = MagicMock(spec=discord.Guild)
    guild.get_channel.side_effect = channels.get
    processor = DiscordQueueProcessor(MagicMock(spec=discord.Client))
    processor._guild = MagicMock(return_value=guild)
    task = await DiscordTask.objects.acreate(
        task_type="broadcast_message", payload={"target": "41-43", "message": "lunch", "sender": "ops"}
    )

    with patch("bot.discord_queue.log_to_ops_channel", new_callable=AsyncMock) as ops:
        await run_twice(processor, task)

    sends = {n: channels[n].channels[0].send.await_count for n in (41, 42, 43)}
    assert sends == {41: 1, 42: 2, 43: 1}
    assert task.result == {"sent_count": 3, "queued_count": 0, "failed_count": 0}
    assert "Sent: 3 channels" in ops.await_args.args[1]


async def test_a_broadcast_retry_resumes_by_team_not_by_position() -> None:
    """Team 45 got it and was then deactivated; the retry still reaches team 46, and only it."""
    from bot.discord_queue import DiscordQueueProcessor

    channels = {}
    for number in (45, 46, 47):
        team = await Team.objects.acreate(
            team_number=number, team_name=f"Team {number}", discord_category_id=number, is_active=True
        )
        chat = MagicMock(spec=discord.TextChannel)
        chat.name = f"team{number}-chat"
        chat.send = AsyncMock(
            side_effect=[discord.HTTPException(MagicMock(status=500), "Internal Server Error"), None]
            if number == 46
            else None
        )
        channels[team.discord_category_id] = MagicMock(spec=discord.CategoryChannel, channels=[chat])
    guild = MagicMock(spec=discord.Guild)
    guild.get_channel.side_effect = channels.get
    processor = DiscordQueueProcessor(MagicMock(spec=discord.Client))
    processor._guild = MagicMock(return_value=guild)
    task = await DiscordTask.objects.acreate(
        task_type="broadcast_message", payload={"target": "all-teams", "message": "lunch", "sender": "ops"}
    )

    with patch("bot.discord_queue.log_to_ops_channel", new_callable=AsyncMock) as ops:
        await processor._process_task(task)
        await Team.objects.filter(team_number=45).aupdate(is_active=False)
        await DiscordTask.objects.filter(pk=task.pk).aupdate(next_retry_at=None)
        await task.arefresh_from_db()
        await processor._process_task(task)

    await task.arefresh_from_db()
    assert task.status == "completed"
    sends = {n: channels[n].channels[0].send.await_count for n in (45, 46, 47)}
    assert sends == {45: 1, 46: 2, 47: 1}
    ops.assert_awaited_once()


def team_guild(sends: dict[int, Any]) -> tuple[MagicMock, dict[int, MagicMock]]:
    """A guild whose team N (made here) has a chat channel whose send has sends[N] as its side effect."""
    chats, categories = {}, {}
    for number, effect in sends.items():
        chats[number] = MagicMock(spec=discord.TextChannel)
        chats[number].name = f"team{number}-chat"
        chats[number].send = AsyncMock(side_effect=effect)
        categories[number] = MagicMock(spec=discord.CategoryChannel, channels=[chats[number]])
    guild = MagicMock(spec=discord.Guild)
    guild.get_channel.side_effect = categories.get
    return guild, chats


async def broadcast_task(guild: MagicMock, target: str) -> tuple[Any, DiscordTask]:
    from bot.discord_queue import DiscordQueueProcessor

    processor = DiscordQueueProcessor(MagicMock(spec=discord.Client))
    processor._guild = MagicMock(return_value=guild)
    task = await DiscordTask.objects.acreate(
        task_type="broadcast_message", payload={"target": target, "message": "lunch", "sender": "ops"}
    )
    return processor, task


async def run_until_done(processor: Any, task: DiscordTask) -> None:
    while task.status not in ("completed", "failed"):
        await DiscordTask.objects.filter(pk=task.pk).aupdate(next_retry_at=None)
        await task.arefresh_from_db()
        await processor._process_task(task)
        await task.arefresh_from_db()


async def test_a_broadcast_goes_past_a_team_that_keeps_failing_and_names_it_at_the_end() -> None:
    for number in (31, 32, 33, 34):
        await Team.objects.acreate(team_number=number, team_name=f"Team {number}", discord_category_id=number)
    down = discord.HTTPException(MagicMock(status=503), "Service Unavailable")
    guild, chats = team_guild({31: None, 32: down, 33: None, 34: None})
    processor, task = await broadcast_task(guild, "31-34")

    with patch("bot.discord_queue.log_to_ops_channel", new_callable=AsyncMock) as ops:
        await run_until_done(processor, task)

    assert task.status == "failed" and task.retry_count == task.max_retries
    assert {n: chats[n].send.await_count for n in chats} == {31: 1, 32: task.max_retries, 33: 1, 34: 1}
    final = ops.await_args.args[1]
    assert "Team 32" in final and "Sent: 3 channels" in final
    assert not any(f"Team {n}" in final for n in (31, 33, 34))


async def test_a_team_discord_refuses_is_not_retried_and_the_rest_are_reached() -> None:
    for number in (35, 36, 37):
        await Team.objects.acreate(team_number=number, team_name=f"Team {number}", discord_category_id=number)
    refused = discord.Forbidden(MagicMock(status=403), "Missing Access")
    guild, chats = team_guild({35: None, 36: refused, 37: None})
    processor, task = await broadcast_task(guild, "35-37")

    with patch("bot.discord_queue.log_to_ops_channel", new_callable=AsyncMock) as ops:
        await run_until_done(processor, task)

    assert task.status == "failed" and task.retry_count == 0
    assert {n: chats[n].send.await_count for n in chats} == {35: 1, 36: 1, 37: 1}
    assert "Team 36" in ops.await_args.args[1]


async def test_a_long_broadcast_retried_sends_each_part_once() -> None:
    await Team.objects.acreate(team_number=38, team_name="Team 38", discord_category_id=38)
    sent: list[str] = []
    guild, chats = team_guild({38: None})
    chats[38].send = flaky_send(sent, fail_on=2)
    processor, task = await broadcast_task(guild, "38")
    await DiscordTask.objects.filter(pk=task.pk).aupdate(payload={"target": "38", "message": LONG, "sender": "ops"})
    await task.arefresh_from_db()

    with patch("bot.discord_queue.log_to_ops_channel", new_callable=AsyncMock):
        await run_twice(processor, task)

    assert len(sent) == len(set(sent)) == 3
    assert task.result == {"sent_count": 1, "queued_count": 0, "failed_count": 0}


async def test_a_team_channel_discord_says_is_gone_is_retried() -> None:
    """A recreate beside the queue can delete the channel between lookup and send; the retry finds the new one."""
    await Team.objects.acreate(team_number=39, team_name="Team 39", discord_category_id=39)
    gone = discord.NotFound(MagicMock(status=404), "Unknown Channel")
    guild, chats = team_guild({39: [gone, None]})
    processor, task = await broadcast_task(guild, "39")

    with patch("bot.discord_queue.log_to_ops_channel", new_callable=AsyncMock):
        await run_twice(processor, task)

    assert chats[39].send.await_count == 2
