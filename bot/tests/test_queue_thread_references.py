"""A ticket's Discord thread can be deleted under it; its reference must not keep tasks failing."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from bot.discord_queue import DiscordQueueProcessor
from bot.tests.discord_fakes import competition_thread
from core.discord_tasks import SyncMemberRoles
from core.models import DiscordTask
from team.models import Team
from ticketing.models import Ticket, TicketCategory

pytestmark = [pytest.mark.asyncio, pytest.mark.django_db(transaction=True)]


@pytest.fixture
async def threaded_ticket(db: Any) -> Ticket:
    team = await Team.objects.acreate(team_number=23, team_name="Team 23", discord_category_id=2300)
    category = await TicketCategory.objects.acreate(pk=91, display_name="Other", points=0, sort_order=91)
    return await Ticket.objects.acreate(
        ticket_number="T023-001",
        team=team,
        category=category,
        title="Help",
        status="open",
        discord_thread_id=2399,
        discord_ticket_message_id=239999,
    )


def _task_for(kind: str, ticket: Ticket, comment_id: int = 0) -> dict[str, Any]:
    payloads = {
        "post_ticket_update": {"ticket_id": ticket.id, "action": "claimed", "actor": "v"},
        "post_comment": {"ticket_id": ticket.id, "comment_id": comment_id},
        "add_user_to_thread": {"ticket_id": ticket.id, "discord_id": 5},
    }
    return {"task_type": kind, "ticket": ticket, "payload": payloads[kind]}


def _bot_after_recreate(chat: MagicMock) -> tuple[MagicMock, MagicMock]:
    """The team's channels were recreated: the stored thread is gone and the new chat channel has none yet."""
    category = MagicMock(spec=discord.CategoryChannel, id=2300, channels=[chat])
    guild = MagicMock(spec=discord.Guild)
    guild.get_channel.side_effect = lambda cid: category if cid == 2300 else None
    guild.get_member.return_value = None
    bot = MagicMock(spec=discord.Client)
    bot.unified_dashboard = MagicMock()
    bot.get_channel.return_value = None
    bot.get_user.return_value = MagicMock(spec=discord.User)
    bot.fetch_channel = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404), "Unknown Channel"))
    return bot, guild


@pytest.mark.parametrize("kind", ["post_ticket_update", "post_comment", "add_user_to_thread"])
async def test_an_open_ticket_whose_thread_was_deleted_gets_a_new_one(threaded_ticket: Ticket, kind: str) -> None:
    from unittest.mock import patch

    new_thread = competition_thread(id=2398, archived=False, locked=False)
    new_thread.send = AsyncMock(return_value=MagicMock(id=239899, pin=AsyncMock()))
    chat = MagicMock(spec=discord.TextChannel)
    chat.name = "team23-chat"
    chat.create_thread = AsyncMock(return_value=new_thread)
    bot, guild = _bot_after_recreate(chat)
    comment = await threaded_ticket.comments.acreate(comment_text="still there?")
    task = await DiscordTask.objects.acreate(**_task_for(kind, threaded_ticket, comment.id))

    with patch("bot.discord_queue.competition_guild", return_value=guild):
        await DiscordQueueProcessor(bot)._process_task(task)

    await task.arefresh_from_db()
    await threaded_ticket.arefresh_from_db()
    assert task.status == "completed", task.error_message
    assert threaded_ticket.discord_thread_id == 2398
    sent = [call.args[0] for call in new_thread.send.await_args_list]
    assert sent[0].startswith("**Ticket #T023-001**")
    if kind == "post_comment":
        assert "still there?" in sent[-1]
    if kind == "add_user_to_thread":
        new_thread.add_user.assert_awaited_with(bot.get_user.return_value)


@pytest.mark.parametrize("kind", ["post_ticket_update", "post_comment", "add_user_to_thread"])
async def test_a_closed_ticket_whose_thread_was_deleted_forgets_it(threaded_ticket: Ticket, kind: str) -> None:
    await Ticket.objects.filter(pk=threaded_ticket.pk).aupdate(status="resolved")
    chat = MagicMock(spec=discord.TextChannel)
    chat.name = "team23-chat"
    chat.create_thread = AsyncMock()
    bot, _guild = _bot_after_recreate(chat)
    comment = await threaded_ticket.comments.acreate(comment_text="still there?")
    task = await DiscordTask.objects.acreate(**_task_for(kind, threaded_ticket, comment.id))

    await DiscordQueueProcessor(bot)._process_task(task)

    await task.arefresh_from_db()
    await threaded_ticket.arefresh_from_db()
    assert task.status == "completed"
    assert threaded_ticket.discord_thread_id is None
    chat.create_thread.assert_not_awaited()


async def test_an_unreachable_thread_is_still_retried(threaded_ticket: Ticket) -> None:
    bot = MagicMock(spec=discord.Client)
    bot.unified_dashboard = MagicMock()
    bot.get_channel.return_value = None
    bot.fetch_channel = AsyncMock(side_effect=discord.HTTPException(MagicMock(status=503), "Service Unavailable"))
    task = await DiscordTask.objects.acreate(
        task_type="post_ticket_update",
        ticket=threaded_ticket,
        payload={"ticket_id": threaded_ticket.id, "action": "claimed", "actor": "v"},
    )

    await DiscordQueueProcessor(bot)._process_task(task)

    await task.arefresh_from_db()
    await threaded_ticket.arefresh_from_db()
    assert task.status == "pending"
    assert threaded_ticket.discord_thread_id == 2399


async def test_adding_a_user_finds_the_thread_from_the_ticket(threaded_ticket: Ticket) -> None:
    """The task's own copy of the thread ID went stale when the thread was recreated; the ticket's is current."""
    thread = competition_thread(id=2399)
    user = MagicMock(spec=discord.User)
    bot = MagicMock(spec=discord.Client)
    bot.get_channel.side_effect = lambda cid: thread if cid == 2399 else None
    bot.get_user.return_value = user
    task = await DiscordTask.objects.acreate(
        task_type="add_user_to_thread",
        ticket=threaded_ticket,
        payload={"ticket_id": threaded_ticket.id, "discord_id": 5, "thread_id": 1},
    )

    await DiscordQueueProcessor(bot)._process_task(task)

    await task.arefresh_from_db()
    assert task.status == "completed"
    thread.add_user.assert_awaited_once_with(user)


@pytest.mark.parametrize("stored", ["text channel", "other guild thread", "hidden thread"])
async def test_a_thread_id_that_isnt_a_competition_thread_fails_at_once(threaded_ticket: Ticket, stored: str) -> None:
    """The Django admin can store any ID; retrying can't make it a competition thread."""
    from unittest.mock import patch

    bot = MagicMock(spec=discord.Client)
    bot.unified_dashboard = MagicMock()
    if stored == "text channel":
        bot.get_channel.return_value = MagicMock(spec=discord.TextChannel)
    elif stored == "other guild thread":
        bot.get_channel.return_value = competition_thread(id=2399)
        bot.get_channel.return_value.guild.id = 1234
    else:
        bot.get_channel.return_value = None
        bot.fetch_channel = AsyncMock(side_effect=discord.Forbidden(MagicMock(status=403), "Missing Access"))
    task = await DiscordTask.objects.acreate(
        task_type="post_ticket_update",
        ticket=threaded_ticket,
        payload={"ticket_id": threaded_ticket.id, "action": "claimed", "actor": "v"},
    )

    with patch("bot.discord_queue.log_to_ops_channel", new_callable=AsyncMock):
        await DiscordQueueProcessor(bot)._process_task(task)

    await task.arefresh_from_db()
    assert (task.status, task.retry_count) == ("failed", 0)
    assert "competition thread" in task.error_message or "hidden from the bot" in task.error_message


async def test_a_member_who_links_later_joins_their_teams_open_ticket_threads(threaded_ticket: Ticket) -> None:
    from unittest.mock import patch

    from django.contrib.auth.models import User

    from team.models import DiscordLink

    user = await User.objects.acreate(username="late-joiner")
    await DiscordLink.objects.acreate(discord_id=2323, discord_username="j", user=user, team_id=threaded_ticket.team_id)
    member = MagicMock(spec=discord.Member, id=2323)
    guild = MagicMock(spec=discord.Guild)
    guild.get_member.return_value = member
    thread = competition_thread(id=2399)
    bot = MagicMock(spec=discord.Client)
    bot.get_channel.side_effect = lambda cid: thread if cid == 2399 else None
    processor = DiscordQueueProcessor(bot)
    processor._guild = MagicMock(return_value=guild)
    stats = {"roles_added": 1, "roles_removed": 0, "errors": 0, "changes": []}

    with patch("bot.discord_queue.sync_member_roles", new=AsyncMock(return_value=stats)):
        await processor._handle_sync_member_roles(SyncMemberRoles(discord_id=2323))

    thread.add_user.assert_awaited_once_with(member)


@pytest.mark.parametrize(
    ("linked", "competition_server", "queued"), [(True, True, 1), (False, True, 0), (True, False, 0)]
)
async def test_a_linked_member_joining_the_server_gets_their_roles_and_threads_queued(
    threaded_ticket: Ticket, settings: Any, linked: bool, competition_server: bool, queued: int
) -> None:
    """Someone who linked before joining had no member to give roles or thread access to until now."""
    from django.contrib.auth.models import User

    from bot.cogs.ticketing import TicketingCog
    from team.models import DiscordLink

    settings.COMPETITION_GUILD_ID = 525
    if linked:
        user = await User.objects.acreate(username="early-linker")
        await DiscordLink.objects.acreate(discord_id=2324, discord_username="e", user=user, team=threaded_ticket.team)
    member = MagicMock(spec=discord.Member, id=2324)
    member.guild.id = 525 if competition_server else 526
    cog = TicketingCog.__new__(TicketingCog)

    await cog.on_member_join(member)

    tasks = [t async for t in DiscordTask.objects.filter(task_type="sync_member_roles")]
    assert [t.payload["discord_id"] for t in tasks] == [2324] * queued
