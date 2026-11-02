"""Tests for utility functions."""

from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from bot.utils import (
    find_team,
    find_team_role,
    log_to_ops_channel,
    team_chat_channel,
)
from team.models import Team


@pytest.mark.django_db(transaction=True)
class TestTeamHelpers:
    """Test team helper functions."""

    async def test_find_team_finds_the_team(self) -> None:
        await Team.objects.filter(team_number=25).adelete()
        await Team.objects.acreate(team_number=25, team_name="Test Team")

        result = await find_team(25)

        assert isinstance(result, Team)
        assert result.team_number == 25

    async def test_find_team_says_a_missing_team_is_not_found(self) -> None:
        assert await find_team(42) == "Team 42 not found"

    async def test_find_team_refuses_a_number_out_of_range(self) -> None:
        assert await find_team(51) == "Team number must be between 1 and 50"


class TestLogging:
    """Test logging utility functions."""

    async def test_log_to_ops_channel_success(self) -> None:
        """Test successfully logging to ops channel."""
        channel = AsyncMock(spec=discord.TextChannel)
        channel.send = AsyncMock()

        bot = MagicMock(spec=discord.Client)
        bot.get_channel = MagicMock(return_value=channel)

        with patch("bot.utils.settings") as mock_settings:
            mock_settings.DISCORD_LOG_CHANNEL_ID = 123456789
            await log_to_ops_channel(bot, "Test message")

        channel.send.assert_called_once_with("Test message")

    async def test_log_to_ops_channel_no_config(self) -> None:
        """Test logging when channel ID not configured."""
        bot = MagicMock(spec=discord.Client)

        with patch("bot.utils.settings") as mock_settings:
            mock_settings.DISCORD_LOG_CHANNEL_ID = None
            await log_to_ops_channel(bot, "Test message")

        bot.get_channel.assert_not_called()

    async def test_log_to_ops_channel_not_found(self) -> None:
        """Test logging when channel not found."""
        bot = MagicMock(spec=discord.Client)
        bot.get_channel = MagicMock(return_value=None)

        with patch("bot.utils.settings") as mock_settings:
            mock_settings.DISCORD_LOG_CHANNEL_ID = 123456789
            await log_to_ops_channel(bot, "Test message")

        # Should not raise exception, just log error
        bot.get_channel.assert_called_once_with(123456789)


def _channel(kind: type, name: str) -> MagicMock:
    channel = MagicMock(spec=kind)
    channel.name = name
    return channel


class TestTeamChatChannel:
    def test_finds_the_chat_text_channel_in_the_teams_category(self) -> None:
        chat = _channel(discord.TextChannel, "Team05-Chat")
        category = MagicMock(spec=discord.CategoryChannel)
        category.channels = [
            _channel(discord.VoiceChannel, "team05-chat"),
            _channel(discord.TextChannel, "notes"),
            chat,
        ]
        guild = MagicMock(spec=discord.Guild)
        guild.get_channel.return_value = category

        assert team_chat_channel(guild, Team(team_number=5, discord_category_id=55)) is chat
        guild.get_channel.assert_called_once_with(55)

    def test_none_without_a_category(self) -> None:
        guild = MagicMock(spec=discord.Guild)
        guild.categories = []
        guild.get_channel.return_value = _channel(discord.TextChannel, "team05-chat")

        assert team_chat_channel(guild, Team(team_number=5)) is None
        assert team_chat_channel(guild, Team(team_number=5, discord_category_id=55)) is None

    def test_finds_the_category_by_name_when_its_id_was_lost(self) -> None:
        """A category whose cleanup failed keeps its name but no longer has its ID recorded."""
        chat = _channel(discord.TextChannel, "team05-chat")
        category = MagicMock(spec=discord.CategoryChannel)
        category.name = "team 05"
        category.channels = [chat]
        guild = MagicMock(spec=discord.Guild)
        guild.categories = [category]

        assert team_chat_channel(guild, Team(team_number=5)) is chat


class TestTeamResourceLookup:
    def test_role_by_id_then_padded_then_unpadded_name(self) -> None:
        by_id, padded, unpadded = (MagicMock(spec=discord.Role) for _ in range(3))
        padded.name, unpadded.name = "Team 07", "Team 7"
        guild = MagicMock(spec=discord.Guild)
        guild.get_role.side_effect = lambda role_id: by_id if role_id == 70 else None
        guild.roles = [unpadded, padded]

        assert find_team_role(guild, Team(team_number=7, discord_role_id=70)) is by_id
        assert find_team_role(guild, Team(team_number=7, discord_role_id=71)) is padded
        guild.roles = [unpadded]
        assert find_team_role(guild, Team(team_number=7)) is unpadded
        assert find_team_role(guild, Team(team_number=8)) is None
