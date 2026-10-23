"""Long ticket text still reaches Discord: Discord rejects a whole message or embed over its limits."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from asgiref.sync import sync_to_async
from hypothesis import given
from hypothesis import strategies as st

from bot.utils import DISCORD_MESSAGE_CHAR_LIMIT, fit, split_message


@given(st.text(alphabet=st.characters(codec="utf-8") | st.just("\n"), max_size=7000))
def test_split_message_keeps_the_text_in_discord_sized_parts(text: str) -> None:
    parts = split_message(text)

    assert parts
    assert all(len(part) <= DISCORD_MESSAGE_CHAR_LIMIT for part in parts)
    # Only the newlines a split happened at are dropped
    assert "".join(parts).replace("\n", "") == text.replace("\n", "")


def test_split_message_breaks_at_line_ends() -> None:
    text = "\n".join(f"line {n:04d} " + "x" * 90 for n in range(40))

    parts = split_message(text)

    assert len(parts) == 3
    assert all(part.startswith("line ") for part in parts)


def test_fit_marks_cut_text() -> None:
    assert fit("abc", 3) == "abc"
    assert fit("abcd", 3) == "ab…"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_web_comment_over_the_message_limit_is_sent_in_parts() -> None:
    from django.contrib.auth.models import User

    from bot.discord_queue import DiscordQueueProcessor
    from core.discord_tasks import PostComment
    from team.models import Team
    from ticketing.models import Ticket, TicketCategory, TicketComment

    team = await Team.objects.acreate(team_number=3, team_name="Team 03")
    category = await TicketCategory.objects.acreate(pk=93, display_name="Other", sort_order=93)
    ticket = await Ticket.objects.acreate(
        ticket_number="T003-001", team=team, category=category, title="Other", discord_thread_id=555
    )
    author = await User.objects.acreate(username="volunteer")
    text = "\n".join("y" * 99 for _ in range(45))
    comment = await TicketComment.objects.acreate(ticket=ticket, author=author, comment_text=text)

    thread = MagicMock(spec=discord.Thread)
    thread.send = AsyncMock(side_effect=lambda content: SimpleNamespace(id=1000 + len(content)))
    bot = MagicMock(spec=discord.Client)
    bot.get_channel.return_value = thread

    await DiscordQueueProcessor(bot)._handle_post_comment(PostComment(ticket_id=ticket.id, comment_id=comment.id))

    sent = [call.args[0] for call in thread.send.call_args_list]
    assert len(sent) == 3
    assert all(len(part) <= DISCORD_MESSAGE_CHAR_LIMIT for part in sent)
    assert sent[0].startswith("**volunteer**\n")
    assert "".join(sent).count("y") == text.count("y")
    await sync_to_async(comment.refresh_from_db)()
    assert comment.discord_message_id == 1000 + len(sent[0])


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_thread_embed_fits_a_long_web_description() -> None:
    from bot.ticket_dashboard import format_ticket_embed
    from team.models import Team
    from ticketing.models import Ticket, TicketCategory

    team = await Team.objects.acreate(team_number=4, team_name="Team 04")
    category = await TicketCategory.objects.acreate(pk=94, display_name="Other", sort_order=94)
    ticket = await Ticket.objects.acreate(
        ticket_number="T004-001", team=team, category=category, title="Other", description="z" * 5000
    )
    ticket = await Ticket.objects.select_related("team", "assigned_to").aget(pk=ticket.pk)

    embed = await sync_to_async(format_ticket_embed)(ticket)

    assert embed.description is not None and len(embed.description) == 4096
    assert embed.description.endswith("…")


@pytest.mark.asyncio
async def test_ticket_reply_fits_a_long_description_and_still_opens_the_thread(mock_interaction: Any) -> None:
    from bot.cogs.ticketing import TicketingCog
    from bot.ticket_input import TicketInput

    description = "w" * 3000
    member = SimpleNamespace(team=SimpleNamespace(team_name="Team 05"))
    ticket = SimpleNamespace(ticket_number="T005-001")
    read = TicketInput(
        SimpleNamespace(display_name="Other", points=0, points_display="0 points"), description, "", "", ""
    )
    cog = TicketingCog.__new__(TicketingCog)
    cog.bot = MagicMock()
    with (
        patch("bot.cogs.ticketing.linked_team_member", AsyncMock(return_value=member)),
        patch("bot.cogs.ticketing.read_ticket", return_value=read),
        patch("bot.cogs.ticketing.acreate_ticket_atomic", AsyncMock(return_value=ticket)),
        patch("bot.cogs.ticketing.publish_new_ticket", AsyncMock()) as publish,
    ):
        await TicketingCog.create_ticket.callback(cog, mock_interaction, category="6", description=description)

    embed = mock_interaction.response.send_message.call_args.kwargs["embed"]
    field = next(f for f in embed.fields if f.name == "Description")
    assert len(field.value) == 1024
    publish.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_thread_embed_fits_a_long_title() -> None:
    from bot.ticket_dashboard import format_ticket_embed
    from team.models import Team
    from ticketing.models import Ticket, TicketCategory

    team = await Team.objects.acreate(team_number=6, team_name="Team 06")
    category = await TicketCategory.objects.acreate(pk=96, display_name="Other", sort_order=96)
    ticket = await Ticket.objects.acreate(ticket_number="T006-001", team=team, category=category, title="t" * 255)
    ticket = await Ticket.objects.select_related("team", "assigned_to").aget(pk=ticket.pk)

    embed = await sync_to_async(format_ticket_embed)(ticket)

    assert embed.title is not None and len(embed.title) == 256
