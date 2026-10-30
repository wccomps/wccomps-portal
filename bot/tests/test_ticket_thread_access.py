"""A ticket thread's messages reach the ticket only from ticket staff and members whose link's seat is its team."""

from unittest.mock import AsyncMock, Mock

import discord
import pytest
from django.contrib.auth.models import User

from bot.cogs.ticketing import TicketingCog
from core.models import UserGroups
from team.models import DiscordLink, Team
from ticketing.models import Ticket, TicketCategory, TicketComment

pytestmark = [pytest.mark.asyncio, pytest.mark.django_db(transaction=True)]


async def _author(discord_id: int, groups: list[str], team: Team | None, *, active: bool = True) -> None:
    user = await User.objects.acreate(username=f"author{discord_id}")
    await UserGroups.objects.acreate(user=user, authentik_id=f"sub{discord_id}", groups=groups, account_active=active)
    await DiscordLink.objects.acreate(
        discord_id=discord_id, discord_username=user.username, user=user, team=team, is_active=True
    )


def _message(discord_id: int, thread_id: int) -> Mock:
    message = AsyncMock(spec=discord.Message)
    message.author = Mock(bot=False, id=discord_id, mention=f"<@{discord_id}>")
    message.id = discord_id * 10
    message.content = f"from {discord_id}"
    message.channel = Mock(spec=discord.Thread, id=thread_id)
    message.attachments = []
    return message


async def test_only_the_teams_members_and_ticket_staff_reach_the_ticket() -> None:
    team = await Team.objects.acreate(team_number=41, team_name="Team 41")
    other = await Team.objects.acreate(team_number=43, team_name="Team 43")
    category = await TicketCategory.objects.acreate(display_name="Other", sort_order=99)
    ticket = await Ticket.objects.acreate(
        ticket_number="T041-001", team=team, category=category, title="t", description="d", discord_thread_id=4100
    )
    await _author(1, [team.authentik_group], team)
    await _author(2, [team.authentik_group], team, active=False)
    await _author(3, [other.authentik_group], other)
    await _author(4, ["WCComps_Ticketing_Support"], None)
    cog = TicketingCog(Mock())
    cog.archive_threads_task.cancel()

    for discord_id in (1, 2, 3, 4, 5):
        await cog.on_message(_message(discord_id, 4100))

    stored = [c async for c in TicketComment.objects.filter(ticket=ticket).values_list("comment_text", flat=True)]
    assert sorted(stored) == ["from 1", "from 4"]
