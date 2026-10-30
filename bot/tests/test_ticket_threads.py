"""A ticket's thread: an open or claimed ticket gets one, with its ticket message, team and assignee, after a
failed send, a lost or cleared thread ID, or a race between two tasks."""

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from bot.tests.discord_fakes import bot_with, recording_thread, ticket_message
from core.models import DiscordTask
from team.models import Team
from ticketing.models import Ticket, TicketCategory

pytestmark = [pytest.mark.asyncio, pytest.mark.django_db(transaction=True)]


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


def chat_guild(ticket: Ticket) -> tuple[MagicMock, MagicMock]:
    chat = MagicMock(spec=discord.TextChannel)
    chat.name = "team24-chat"
    category = MagicMock(spec=discord.CategoryChannel, id=2400, channels=[chat])
    guild = MagicMock(spec=discord.Guild)
    guild.get_channel.side_effect = lambda cid: category if cid == 2400 else None
    guild.get_member.return_value = None
    return guild, chat


def ticket_messages(thread: MagicMock) -> list[str]:
    return [t for t in thread.texts if t.startswith("**Ticket #T024-001**")]


async def threadless(ticket: Ticket) -> Ticket:
    await Ticket.objects.filter(pk=ticket.pk).aupdate(discord_thread_id=None)
    return await Ticket.objects.select_related("team").aget(pk=ticket.pk)


async def test_a_web_ticket_whose_thread_failed_is_retried(ticket: Ticket) -> None:
    from bot.discord_queue import DiscordQueueProcessor

    ticket = await threadless(ticket)
    guild, chat = chat_guild(ticket)
    thread = recording_thread(2498)
    chat.create_thread = AsyncMock(
        side_effect=[discord.HTTPException(MagicMock(status=500), "Internal Server Error"), thread]
    )
    task = await DiscordTask.objects.acreate(
        task_type="ticket_created_web", ticket=ticket, payload={"ticket_id": ticket.id}
    )

    with patch("bot.discord_queue.competition_guild", return_value=guild):
        await run_twice(DiscordQueueProcessor(bot_with(thread)), task)

    await ticket.arefresh_from_db()
    assert ticket.discord_thread_id == 2498
    assert len(ticket_messages(thread)) == 1


async def test_a_web_ticket_whose_message_failed_gets_it_in_the_same_thread(ticket: Ticket) -> None:
    from bot.discord_queue import DiscordQueueProcessor

    ticket = await threadless(ticket)
    guild, chat = chat_guild(ticket)
    thread = recording_thread(2497, failing_sends={1})
    chat.create_thread = AsyncMock(return_value=thread)
    task = await DiscordTask.objects.acreate(
        task_type="ticket_created_web", ticket=ticket, payload={"ticket_id": ticket.id}
    )

    with patch("bot.discord_queue.competition_guild", return_value=guild):
        await run_twice(DiscordQueueProcessor(bot_with(thread)), task)

    chat.create_thread.assert_awaited_once()
    assert len(ticket_messages(thread)) == 1


async def test_a_comment_during_the_web_tickets_backoff_leaves_one_ticket_message(ticket: Ticket) -> None:
    """The comment makes the thread while the web ticket's own task waits to retry; the retry finds the message."""
    from bot.discord_queue import DiscordQueueProcessor

    ticket = await threadless(ticket)
    guild, chat = chat_guild(ticket)
    thread = recording_thread(2494)
    chat.create_thread = AsyncMock(
        side_effect=[discord.HTTPException(MagicMock(status=500), "Internal Server Error"), thread]
    )
    created = await DiscordTask.objects.acreate(
        task_type="ticket_created_web", ticket=ticket, payload={"ticket_id": ticket.id}
    )
    comment = await ticket.comments.acreate(comment_text="any news?")
    commented = await DiscordTask.objects.acreate(
        task_type="post_comment", ticket=ticket, payload={"ticket_id": ticket.id, "comment_id": comment.id}
    )
    processor = DiscordQueueProcessor(bot_with(thread))

    with patch("bot.discord_queue.competition_guild", return_value=guild):
        await processor._process_task(created)
        await processor._process_task(commented)
        await DiscordTask.objects.filter(pk=created.pk).aupdate(next_retry_at=None)
        await processor._process_task(created)

    await created.arefresh_from_db()
    assert created.status == "completed"
    assert len(ticket_messages(thread)) == 1
    assert any("any news?" in text for text in thread.texts)


async def test_a_recovered_thread_whose_ticket_message_failed_gets_it_on_retry(ticket: Ticket) -> None:
    """A comment made the thread, then sending the ticket message failed; the retry finds the thread and posts it."""
    from bot.discord_queue import DiscordQueueProcessor

    ticket = await threadless(ticket)
    guild, chat = chat_guild(ticket)
    thread = recording_thread(2493, failing_sends={1})
    chat.create_thread = AsyncMock(return_value=thread)
    comment = await ticket.comments.acreate(comment_text="any news?")
    task = await DiscordTask.objects.acreate(
        task_type="post_comment", ticket=ticket, payload={"ticket_id": ticket.id, "comment_id": comment.id}
    )

    with patch("bot.discord_queue.competition_guild", return_value=guild):
        await run_twice(DiscordQueueProcessor(bot_with(thread)), task)

    chat.create_thread.assert_awaited_once()
    assert len(ticket_messages(thread)) == 1
    assert "any news?" in thread.texts[-1]


async def test_a_ticket_resolved_before_its_thread_task_runs_gets_no_thread(ticket: Ticket) -> None:
    from bot.discord_queue import DiscordQueueProcessor

    ticket = await threadless(ticket)
    await Ticket.objects.filter(pk=ticket.pk).aupdate(status="resolved")
    guild, chat = chat_guild(ticket)
    chat.create_thread = AsyncMock()
    task = await DiscordTask.objects.acreate(
        task_type="ticket_created_web", ticket=ticket, payload={"ticket_id": ticket.id}
    )

    with patch("bot.discord_queue.competition_guild", return_value=guild):
        await DiscordQueueProcessor(bot_with())._process_task(task)

    await task.arefresh_from_db()
    assert task.status == "completed"
    chat.create_thread.assert_not_awaited()


@pytest.mark.parametrize("winner_stored_its_message", [False, True])
async def test_a_thread_made_by_another_task_meanwhile_wins(ticket: Ticket, winner_stored_its_message: bool) -> None:
    """Two tasks made a thread at once: the one stored first is the ticket's, the other is deleted, and the winner's
    ticket message is used, not posted again."""
    from bot.thread_creator import ensure_ticket_thread

    ticket = await threadless(ticket)
    guild, chat = chat_guild(ticket)
    stored = recording_thread(2492, pinned_text=ticket_message(ticket.ticket_number))
    extra = recording_thread(2491)

    async def create_thread(**_kwargs: Any) -> MagicMock:
        message_id = 249299 if winner_stored_its_message else None
        await Ticket.objects.filter(pk=ticket.pk).aupdate(
            discord_thread_id=stored.id, discord_ticket_message_id=message_id
        )
        return extra

    chat.create_thread = AsyncMock(side_effect=create_thread)

    thread = await ensure_ticket_thread(bot_with(stored, extra), guild, ticket)

    assert thread is stored
    extra.delete.assert_awaited_once()
    assert stored.texts == [] and extra.texts == []
    await ticket.arefresh_from_db()
    assert (ticket.discord_thread_id, ticket.discord_ticket_message_id) == (stored.id, 249299)


async def test_a_thread_from_before_the_message_id_was_stored_keeps_its_message(ticket: Ticket) -> None:
    """A thread posted with the earlier wording ("- Use buttons below") gets that message adopted, not a second."""
    from bot.thread_creator import ensure_ticket_thread

    guild, chat = chat_guild(ticket)
    old = f"**Ticket #{ticket.ticket_number}** - Use buttons below to manage this ticket."
    thread = recording_thread(2499, pinned_text=old)
    chat.create_thread = AsyncMock()
    ticket = await Ticket.objects.select_related("team").aget(pk=ticket.pk)

    await ensure_ticket_thread(bot_with(thread), guild, ticket)
    await ensure_ticket_thread(bot_with(thread), guild, ticket)

    assert thread.texts == []
    thread.pins.assert_called_once()
    await ticket.arefresh_from_db()
    assert ticket.discord_ticket_message_id == 249999


async def _team_member(ticket: Ticket, guild: MagicMock) -> MagicMock:
    from django.contrib.auth.models import User

    from team.models import DiscordLink

    user = await User.objects.acreate(username="team24member")
    await DiscordLink.objects.acreate(discord_id=2424, discord_username="m", user=user, team_id=ticket.team_id)
    member = MagicMock(spec=discord.Member, id=2424)
    guild.get_member.side_effect = lambda mid: member if mid == 2424 else None
    return member


async def test_a_thread_without_its_ticket_message_gets_it_and_the_team(ticket: Ticket) -> None:
    from bot.thread_creator import ensure_ticket_thread

    guild, chat = chat_guild(ticket)
    member = await _team_member(ticket, guild)
    thread = recording_thread(2499)
    chat.create_thread = AsyncMock()
    ticket = await Ticket.objects.select_related("team").aget(pk=ticket.pk)

    await ensure_ticket_thread(bot_with(thread), guild, ticket)
    await ensure_ticket_thread(bot_with(thread), guild, ticket)

    chat.create_thread.assert_not_awaited()
    thread.add_user.assert_awaited_once_with(member)
    assert len(ticket_messages(thread)) == 1


async def test_a_web_ticket_whose_stored_thread_is_gone_gets_a_new_one(ticket: Ticket) -> None:
    from bot.discord_queue import DiscordQueueProcessor

    guild, chat = chat_guild(ticket)
    member = await _team_member(ticket, guild)
    thread = recording_thread(2496)
    chat.create_thread = AsyncMock(return_value=thread)
    task = await DiscordTask.objects.acreate(
        task_type="ticket_created_web", ticket=ticket, payload={"ticket_id": ticket.id}
    )

    with patch("bot.discord_queue.competition_guild", return_value=guild):
        await DiscordQueueProcessor(bot_with())._process_task(task)

    await task.arefresh_from_db()
    await ticket.arefresh_from_db()
    assert task.status == "completed"
    assert ticket.discord_thread_id == 2496
    thread.add_user.assert_awaited_once_with(member)
    assert len(ticket_messages(thread)) == 1


async def test_a_new_thread_gets_the_assignee_too(ticket: Ticket) -> None:
    """Someone who claimed the ticket before its thread existed was never invited to it."""
    from django.contrib.auth.models import User

    from bot.thread_creator import ensure_ticket_thread
    from team.models import DiscordLink

    ticket = await threadless(ticket)
    guild, chat = chat_guild(ticket)
    member = await _team_member(ticket, guild)
    volunteer = await User.objects.acreate(username="early-claimer")
    await DiscordLink.objects.acreate(discord_id=2525, discord_username="v", user=volunteer)
    await Ticket.objects.filter(pk=ticket.pk).aupdate(assigned_to=volunteer, status="claimed")
    claimer = MagicMock(spec=discord.Member, id=2525)
    guild.get_member.side_effect = lambda mid: {2424: member, 2525: claimer}.get(mid)
    thread = recording_thread(2495)
    chat.create_thread = AsyncMock(return_value=thread)
    ticket = await Ticket.objects.select_related("team").aget(pk=ticket.pk)

    await ensure_ticket_thread(bot_with(), guild, ticket)

    assert {call.args[0] for call in thread.add_user.await_args_list} == {member, claimer}


async def test_a_thread_id_staff_cleared_gets_a_new_thread_with_its_message_and_team(ticket: Ticket) -> None:
    """The recorded message was in the old thread; it must not stop the new one getting its own."""
    from bot.thread_creator import ensure_ticket_thread

    await Ticket.objects.filter(pk=ticket.pk).aupdate(discord_ticket_message_id=249999)
    ticket = await threadless(ticket)
    guild, chat = chat_guild(ticket)
    member = await _team_member(ticket, guild)
    thread = recording_thread(2490, failing_sends={1})
    chat.create_thread = AsyncMock(return_value=thread)

    with pytest.raises(discord.HTTPException):
        await ensure_ticket_thread(bot_with(), guild, ticket)
    ticket = await Ticket.objects.select_related("team").aget(pk=ticket.pk)
    await ensure_ticket_thread(bot_with(thread), guild, ticket)

    assert len(ticket_messages(thread)) == 1
    thread.add_user.assert_awaited_with(member)
    await ticket.arefresh_from_db()
    assert ticket.discord_ticket_message_id == thread.messages[0].id


async def test_changing_the_thread_id_in_the_admin_forgets_the_ticket_message(ticket: Ticket) -> None:
    from django.contrib.admin.sites import site
    from django.test import RequestFactory

    await Ticket.objects.filter(pk=ticket.pk).aupdate(discord_ticket_message_id=249999)
    ticket = await Ticket.objects.aget(pk=ticket.pk)
    ticket.discord_thread_id = 2488
    form = MagicMock(changed_data=["discord_thread_id"])

    from asgiref.sync import sync_to_async

    await sync_to_async(site._registry[Ticket].save_model)(RequestFactory().post("/"), ticket, form, True)

    await ticket.arefresh_from_db()
    assert (ticket.discord_thread_id, ticket.discord_ticket_message_id) == (2488, None)


async def test_an_adopted_older_thread_gets_the_team_once(ticket: Ticket) -> None:
    """A thread from before the message ID was stored may never have had the team's members added."""
    from bot.thread_creator import ensure_ticket_thread

    guild, chat = chat_guild(ticket)
    member = await _team_member(ticket, guild)
    thread = recording_thread(2499, pinned_text=ticket_message(ticket.ticket_number))
    chat.create_thread = AsyncMock()
    ticket = await Ticket.objects.select_related("team").aget(pk=ticket.pk)

    await ensure_ticket_thread(bot_with(thread), guild, ticket)
    await ensure_ticket_thread(bot_with(thread), guild, ticket)

    thread.add_user.assert_awaited_once_with(member)
    assert thread.texts == []


async def test_a_claim_made_while_the_thread_was_created_brings_the_claimer_in(ticket: Ticket) -> None:
    from django.contrib.auth.models import User

    from bot.thread_creator import ensure_ticket_thread
    from team.models import DiscordLink

    ticket = await threadless(ticket)
    guild, chat = chat_guild(ticket)
    volunteer = await User.objects.acreate(username="quick-claimer")
    await DiscordLink.objects.acreate(discord_id=2526, discord_username="q", user=volunteer)
    claimer = MagicMock(spec=discord.Member, id=2526)
    guild.get_member.side_effect = lambda mid: claimer if mid == 2526 else None
    thread = recording_thread(2489)

    async def create_thread(**_kwargs: Any) -> MagicMock:
        await Ticket.objects.filter(pk=ticket.pk).aupdate(assigned_to=volunteer, status="claimed")
        return thread

    chat.create_thread = AsyncMock(side_effect=create_thread)

    await ensure_ticket_thread(bot_with(), guild, ticket)

    thread.add_user.assert_awaited_once_with(claimer)


async def test_a_team_account_assignee_is_not_pulled_into_the_thread(ticket: Ticket) -> None:
    """An old ticket assigned to another team's account must not bring that team into this thread."""
    from django.contrib.auth.models import User

    from bot.thread_creator import ensure_ticket_thread
    from core.models import UserGroups
    from team.models import DiscordLink

    other = await Team.objects.acreate(team_number=25, team_name="Team 25", authentik_group="WCComps_BlueTeam25")
    account = await User.objects.acreate(username="team25")
    await UserGroups.objects.acreate(user=account, authentik_id="team25-uid", groups=[other.authentik_group])
    await DiscordLink.objects.acreate(discord_id=2527, discord_username="t", user=account, team=other)
    await Ticket.objects.filter(pk=ticket.pk).aupdate(assigned_to=account, status="claimed")
    ticket = await threadless(ticket)
    guild, chat = chat_guild(ticket)
    outsider = MagicMock(spec=discord.Member, id=2527)
    guild.get_member.side_effect = lambda mid: outsider if mid == 2527 else None
    thread = recording_thread(2487)
    chat.create_thread = AsyncMock(return_value=thread)

    await ensure_ticket_thread(bot_with(), guild, ticket)

    thread.add_user.assert_not_awaited()


async def test_a_lost_race_finds_the_winners_message_on_a_second_look(ticket: Ticket) -> None:
    """The winner stored its message while this task looked through the thread; it must not post another."""
    from bot.thread_creator import ensure_ticket_thread

    ticket = await threadless(ticket)
    guild, chat = chat_guild(ticket)
    stored, extra = recording_thread(2486), recording_thread(2485)

    async def create_thread(**_kwargs: Any) -> MagicMock:
        await Ticket.objects.filter(pk=ticket.pk).aupdate(discord_thread_id=stored.id)
        return extra

    async def winner_posts(**_kwargs: Any) -> Any:
        await Ticket.objects.filter(pk=ticket.pk).aupdate(discord_ticket_message_id=248699)
        return
        yield

    chat.create_thread = AsyncMock(side_effect=create_thread)
    stored.pins = MagicMock(side_effect=winner_posts)

    await ensure_ticket_thread(bot_with(stored, extra), guild, ticket)

    assert stored.texts == []


async def test_a_lost_race_waits_for_a_slow_winners_message(ticket: Ticket, monkeypatch: pytest.MonkeyPatch) -> None:
    """The winner is still posting its message when this task finds the thread empty; it waits rather than post."""
    from bot import thread_creator

    monkeypatch.setattr(thread_creator, "RACE_POLL_SECONDS", 0.05)
    ticket = await threadless(ticket)
    guild, chat = chat_guild(ticket)
    stored, extra = recording_thread(2484), recording_thread(2483)

    async def winner_posts_later() -> None:
        await asyncio.sleep(0.2)
        await Ticket.objects.filter(pk=ticket.pk).aupdate(discord_ticket_message_id=248499)

    async def create_thread(**_kwargs: Any) -> MagicMock:
        await Ticket.objects.filter(pk=ticket.pk).aupdate(discord_thread_id=stored.id)
        background.append(asyncio.create_task(winner_posts_later()))
        return extra

    background: list[asyncio.Task[None]] = []
    chat.create_thread = AsyncMock(side_effect=create_thread)

    await thread_creator.ensure_ticket_thread(bot_with(stored, extra), guild, ticket)
    await asyncio.gather(*background)

    assert stored.texts == [] and extra.texts == []


async def test_a_new_thread_is_private_and_its_members_cant_invite(ticket: Ticket) -> None:
    from bot.thread_creator import ensure_ticket_thread

    ticket = await threadless(ticket)
    guild, chat = chat_guild(ticket)
    chat.create_thread = AsyncMock(return_value=recording_thread(2482))

    await ensure_ticket_thread(bot_with(), guild, ticket)

    kwargs = chat.create_thread.await_args.kwargs
    assert kwargs["invitable"] is False and kwargs.get("type") in (None, discord.ChannelType.private_thread)
