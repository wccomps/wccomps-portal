"""A broadcast lists each recipient it couldn't reach, and the queue retries one that reached no one."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from django.test import override_settings

from bot.competition_actions import deliver_broadcast
from bot.discord_queue import DiscordQueueProcessor
from core.broadcast import Announcements
from core.discord_tasks import BroadcastMessage

pytestmark = pytest.mark.asyncio


def _guild(channel: Any) -> MagicMock:
    guild = MagicMock(spec=discord.Guild)
    guild.get_channel.return_value = channel
    guild.get_role.return_value = None
    return guild


@override_settings(DISCORD_ANNOUNCEMENT_CHANNEL_ID=31, BLUETEAM_ROLE_ID=0)
async def test_a_refused_announcement_is_listed_as_failed() -> None:
    channel = MagicMock(spec=discord.TextChannel)
    channel.send = AsyncMock(side_effect=discord.Forbidden(MagicMock(status=403), "Missing Access"))

    result = await deliver_broadcast(_guild(channel), Announcements(), "hello", "ops")

    assert result.sent == 0
    assert result.failed and result.failed[0].startswith("Announcements")


@override_settings(DISCORD_ANNOUNCEMENT_CHANNEL_ID=31)
async def test_a_missing_announcements_channel_is_listed_as_failed() -> None:
    result = await deliver_broadcast(_guild(None), Announcements(), "hello", "ops")

    assert "Announcements channel not found" in result.failed[0]


@override_settings(DISCORD_ANNOUNCEMENT_CHANNEL_ID=31)
async def test_the_queue_retries_a_broadcast_that_reached_no_one() -> None:
    processor = DiscordQueueProcessor(MagicMock(spec=discord.Client))
    processor._guild = MagicMock(return_value=_guild(None))

    with (
        patch("bot.discord_queue.log_to_ops_channel", new_callable=AsyncMock),
        pytest.raises(RuntimeError, match="reached no one"),
    ):
        await processor._handle_broadcast_message(BroadcastMessage(target="announcements", message="hi", sender="ops"))
