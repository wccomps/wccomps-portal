"""Replies built from lists must fit Discord's 2,000-character message limit."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from bot.utils import DISCORD_MESSAGE_CHAR_LIMIT, log_to_ops_channel, send_lines

LONG_LINES = [
    f"[DRY RUN] Would add role WCComps Operations Team to member-{i:03d} (linked as someone)" for i in range(122)
]


@pytest.mark.asyncio
async def test_long_list_fits_one_message_and_attaches_everything() -> None:
    interaction = MagicMock()
    interaction.followup.send = AsyncMock()

    await send_lines(interaction, "**Header**", LONG_LINES, title="Changes", filename="changes.txt")

    kwargs = interaction.followup.send.await_args.kwargs
    text = interaction.followup.send.await_args.args[0]
    assert len(text) <= DISCORD_MESSAGE_CHAR_LIMIT
    assert "more (full list attached)" in text
    attached = kwargs["file"].fp.getvalue().decode().splitlines()
    assert attached == LONG_LINES


@pytest.mark.asyncio
async def test_short_list_is_sent_whole_without_a_file() -> None:
    interaction = MagicMock()
    interaction.followup.send = AsyncMock()

    await send_lines(interaction, "**Header**", ["a", "b"], title="Changes", filename="changes.txt")

    assert interaction.followup.send.await_args.args[0] == "**Header**\n\n**Changes:**\na\nb\n"
    assert "file" not in interaction.followup.send.await_args.kwargs


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_sync_roles_preview_with_many_changes(mock_interaction: Any, mock_bot: Any) -> None:
    from bot.cogs.admin import AdminCog

    stats = {"roles_added": 2, "roles_removed": 0, "errors": 0, "changes": LONG_LINES}
    with (
        patch("bot.role_sync.competition_guild", return_value=MagicMock()),
        patch("bot.role_sync.sync_roles", new=AsyncMock(return_value=stats)),
        patch("bot.cogs.admin.log_to_ops_channel", new_callable=AsyncMock),
    ):
        cog = AdminCog(mock_bot)
        await cog.admin_sync_roles.callback(cog, mock_interaction)

    result = mock_interaction.followup.send.await_args
    assert len(result.args[0]) <= DISCORD_MESSAGE_CHAR_LIMIT
    assert "file" in result.kwargs


@pytest.mark.asyncio
async def test_ops_channel_messages_are_split_to_the_limit_and_lose_nothing() -> None:
    channel = MagicMock(spec=discord.TextChannel)
    channel.send = AsyncMock()
    bot = MagicMock()
    bot.get_channel.return_value = channel
    message = "\n".join(f"- **Team {n}**: web-http, ssh, dns" for n in range(1, 151))

    with patch("bot.utils.settings") as settings:
        settings.DISCORD_LOG_CHANNEL_ID = 1
        assert await log_to_ops_channel(bot, message)

    sent = [c.args[0] for c in channel.send.await_args_list]
    assert len(sent) > 1
    assert all(len(part) <= DISCORD_MESSAGE_CHAR_LIMIT for part in sent)
    assert "\n".join(sent) == message
