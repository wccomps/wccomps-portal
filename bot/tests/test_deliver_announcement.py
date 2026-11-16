"""A broadcast lists each recipient it couldn't reach; the queue retries a failure that may pass, and a refusal
fails the task at once."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from django.test import override_settings

from bot.discord_queue import DiscordQueueProcessor
from core.models import QueuedTask

pytestmark = [pytest.mark.asyncio, pytest.mark.django_db(transaction=True)]


def _guild(channel: Any) -> MagicMock:
    guild = MagicMock(spec=discord.Guild)
    guild.get_channel.return_value = channel
    guild.get_role.return_value = None
    return guild


async def _announce(guild: MagicMock) -> QueuedTask:
    """Run a broadcast to the announcements channel through the queue; its row once it ran."""
    processor = DiscordQueueProcessor(MagicMock(spec=discord.Client))
    processor._guild = MagicMock(return_value=guild)
    task = await QueuedTask.objects.acreate(
        task_type="broadcast_message", payload={"target": "announcements", "message": "hi", "sender": "ops"}
    )
    with patch("bot.discord_queue.log_to_ops_channel", new_callable=AsyncMock):
        await processor._process_task(task)
    await task.arefresh_from_db()
    return task


@override_settings(DISCORD_ANNOUNCEMENT_CHANNEL_ID=31, BLUETEAM_ROLE_ID=0)
async def test_a_refused_announcement_fails_the_task_at_once() -> None:
    channel = MagicMock(spec=discord.TextChannel)
    channel.send = AsyncMock(side_effect=discord.Forbidden(MagicMock(status=403), "Missing Access"))

    task = await _announce(_guild(channel))

    assert (task.status, task.retry_count) == ("failed", 0)
    assert "• Sent: 0 channels" in task.error_message
    assert "Failed: Announcements (" in task.error_message


@override_settings(DISCORD_ANNOUNCEMENT_CHANNEL_ID=31)
async def test_a_missing_announcements_channel_fails_the_task_at_once() -> None:
    task = await _announce(_guild(None))

    assert (task.status, task.retry_count) == ("failed", 0)
    assert "Failed: Announcements (Announcements channel not found" in task.error_message
