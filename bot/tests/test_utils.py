"""Tests for utility functions."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from django.contrib.auth.models import User

from bot.utils import (
    get_team_member_discord_ids,
    get_team_or_respond,
    log_to_ops_channel,
    team_chat_channel,
)
from team.models import DiscordLink, Team


@pytest.mark.django_db(transaction=True)
class TestTeamHelpers:
    """Test team helper functions."""

    async def test_get_team_or_respond_success(self, mock_interaction: Any) -> None:
        """Test successfully getting a team."""
        team_num = 25

        # Delete if exists
        await Team.objects.filter(team_number=team_num).adelete()

        await Team.objects.acreate(
            team_number=team_num,
            team_name="Test Team",
        )

        result = await get_team_or_respond(mock_interaction, team_num)

        assert result is not None
        assert result.team_number == team_num
        mock_interaction.response.send_message.assert_not_called()

    async def test_get_team_or_respond_not_found(self, mock_interaction: Any) -> None:
        """Test handling team not found."""
        result = await get_team_or_respond(mock_interaction, 42)

        assert result is None
        mock_interaction.response.send_message.assert_called_once()
        call_args = mock_interaction.response.send_message.call_args
        assert "not found" in call_args.args[0].lower()

    async def test_get_team_or_respond_invalid_range(self, mock_interaction: Any) -> None:
        """Test handling team number out of range."""
        result = await get_team_or_respond(mock_interaction, 51)

        assert result is None
        mock_interaction.response.send_message.assert_called_once()
        call_args = mock_interaction.response.send_message.call_args
        assert "between 1 and 50" in call_args.args[0]

    async def test_get_team_member_discord_ids(self) -> None:
        """Test getting Discord IDs for team members."""
        team_num = 26

        # Delete if exists
        await Team.objects.filter(team_number=team_num).adelete()

        team = await Team.objects.acreate(
            team_number=team_num,
            team_name="Test Team",
        )

        # Create users for the discord links
        user1 = await User.objects.acreate(username="user1")
        user2 = await User.objects.acreate(username="user2")
        user3 = await User.objects.acreate(username="user3")

        # Create active and inactive members
        await DiscordLink.objects.acreate(
            discord_id=111111111,
            discord_username="user1#0001",
            user=user1,
            team=team,
            is_active=True,
        )
        await DiscordLink.objects.acreate(
            discord_id=222222222,
            discord_username="user2#0002",
            user=user2,
            team=team,
            is_active=True,
        )
        await DiscordLink.objects.acreate(
            discord_id=333333333,
            discord_username="user3#0003",
            user=user3,
            team=team,
            is_active=False,  # Inactive
        )

        discord_ids = await get_team_member_discord_ids(team)

        assert len(discord_ids) == 2
        assert 111111111 in discord_ids
        assert 222222222 in discord_ids
        assert 333333333 not in discord_ids


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
        guild.get_channel.return_value = _channel(discord.TextChannel, "team05-chat")

        assert team_chat_channel(guild, Team(team_number=5)) is None
        assert team_chat_channel(guild, Team(team_number=5, discord_category_id=55)) is None
