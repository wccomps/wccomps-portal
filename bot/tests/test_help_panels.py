"""Help panel ticket flow, run on the event loop with Django's async-safety check on."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import discord
import pytest
from django.contrib.auth.models import User

from bot.cogs.help_panels import (
    CategorySelect,
    HelpPanelsCog,
    TeamHelpView,
    TicketModal,
    create_ticket,
)
from bot.discord_queue import DiscordQueueProcessor
from core.models import DiscordTask, UserGroups
from team.models import DiscordLink, Team
from ticketing.models import Ticket, TicketCategory

pytestmark = [pytest.mark.asyncio, pytest.mark.django_db(transaction=True)]


@pytest.fixture
async def categories(db: Any) -> tuple[TicketCategory, TicketCategory]:
    await TicketCategory.objects.all().adelete()
    consult = await TicketCategory.objects.acreate(
        pk=91,
        display_name="Consultation",
        points=5,
        required_fields=["description"],
        optional_fields=["hostname"],
        user_creatable=True,
        sort_order=1,
    )
    hidden = await TicketCategory.objects.acreate(
        pk=92,
        display_name="Staff Only",
        points=0,
        required_fields=["description"],
        user_creatable=False,
        sort_order=2,
    )
    return consult, hidden


async def test_create_ticket_button_lists_user_creatable_categories(
    categories: tuple[TicketCategory, TicketCategory], mock_interaction: Any, mock_bot: Any
) -> None:
    consult, _ = categories
    await TeamHelpView(mock_bot).create_ticket(mock_interaction)

    view = mock_interaction.response.send_message.await_args.kwargs["view"]
    select = view.children[0]
    assert [o.value for o in select.options] == [str(consult.pk)]


async def test_create_ticket_button_handles_no_categories(mock_interaction: Any, mock_bot: Any) -> None:
    await TicketCategory.objects.all().adelete()
    await TeamHelpView(mock_bot).create_ticket(mock_interaction)

    mock_interaction.response.send_message.assert_awaited_once_with(
        "No ticket categories are currently available.",
        ephemeral=True,
    )


async def test_category_select_opens_modal_without_db(categories: tuple[TicketCategory, TicketCategory]) -> None:
    consult, _ = categories
    select = CategorySelect([consult])
    interaction = AsyncMock(spec=discord.Interaction)
    interaction.response = AsyncMock()

    with patch.object(CategorySelect, "values", new=[str(consult.pk)]):
        await select.callback(interaction)

    modal = interaction.response.send_modal.await_args.args[0]
    assert isinstance(modal, TicketModal)
    assert modal.title == "Consultation"


async def test_help_panel_ticket_queues_its_thread_and_the_queue_makes_it(
    categories: tuple[TicketCategory, TicketCategory], mock_interaction: Any, mock_bot: Any
) -> None:
    consult, _ = categories
    team = await Team.objects.acreate(team_number=12, team_name="Team 12", discord_category_id=4242)
    member = await User.objects.acreate(username="team12member")
    await UserGroups.objects.acreate(user=member, authentik_id="team12member-uid", groups=[team.authentik_group])
    await DiscordLink.objects.acreate(
        user=member, discord_id=mock_interaction.user.id, discord_username="u", team=team, is_active=True
    )

    thread = MagicMock(spec=discord.Thread)
    thread.id = 777
    thread.send = AsyncMock()
    thread.add_user = AsyncMock()
    chat = Mock(spec=discord.TextChannel)
    chat.name = "team-chat"
    chat.create_thread = AsyncMock(return_value=thread)
    chat.threads = []
    category = Mock(spec=discord.CategoryChannel)
    category.id = 4242
    category.name = "Team 12"
    category.channels = [chat]
    mock_interaction.guild.get_channel = Mock(return_value=category)
    mock_interaction.guild.get_member = Mock(return_value=None)
    mock_interaction.client = mock_bot
    mock_bot.get_guild = Mock(return_value=mock_interaction.guild)

    await create_ticket(mock_interaction, category_id=str(consult.pk), description="help with dns")

    assert "created" in mock_interaction.followup.send.await_args.args[0]
    ticket = await Ticket.objects.aget(team=team)
    assert ticket.discord_thread_id is None
    task = await DiscordTask.objects.aget(task_type="ticket_created_web")
    assert task.ticket_id == ticket.pk

    with patch("bot.discord_queue.trigger_dashboard") as dashboard:
        await DiscordQueueProcessor(mock_bot)._process_task(task)

    await ticket.arefresh_from_db()
    assert ticket.discord_thread_id == 777
    assert ticket.ticket_number in thread.send.await_args.kwargs["embed"].title
    dashboard.assert_called()


async def test_post_ticket_panel_lists_categories(
    categories: tuple[TicketCategory, TicketCategory], mock_bot: Any
) -> None:
    channel = MagicMock(spec=discord.TextChannel)
    channel.history = MagicMock(return_value=_empty_history())
    channel.send = AsyncMock()
    mock_bot.get_channel = Mock(return_value=channel)

    await HelpPanelsCog(mock_bot)._post_ticket_panel(1)

    embed = channel.send.await_args.kwargs["embed"]
    assert "Consultation" in embed.description
    assert "Staff Only" not in embed.description


async def _empty_history() -> Any:
    for _ in ():
        yield
