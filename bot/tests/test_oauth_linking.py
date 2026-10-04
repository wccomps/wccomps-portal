"""Tests for link token generation and OAuth callback flow."""

from datetime import timedelta
from typing import Any
from unittest.mock import patch

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from bot.cogs.linking import LinkingCog
from team.models import DiscordLink, LinkRateLimit, LinkToken, Team


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
class TestLinkTokenGeneration:
    """Test link token generation via /link command."""

    async def test_link_command_generates_token(self, mock_interaction: Any, mock_bot: Any) -> None:
        """Test that /link command creates valid token."""
        cog = LinkingCog(mock_bot)

        with patch("bot.cogs.linking.settings") as mock_settings:
            mock_settings.BASE_URL = "https://test.example.com"
            await cog.link_command.callback(cog, mock_interaction)

        # Verify response was sent
        mock_interaction.followup.send.assert_called_once()

        # Verify token was created
        token = await LinkToken.objects.filter(discord_id=mock_interaction.user.id).afirst()
        assert token is not None
        assert token.discord_username == str(mock_interaction.user)
        assert not token.used
        assert token.expires_at > timezone.now()

        # Verify embed contains auth URL
        call_args = mock_interaction.followup.send.call_args
        embed = call_args.kwargs.get("embed")
        assert embed is not None
        assert f"token={token.token}" in embed.description

    async def test_link_command_already_linked(self, mock_interaction: Any, mock_bot: Any) -> None:
        """Test that /link command rejects already linked users."""
        team = await Team.objects.acreate(
            team_number=10,
            team_name="Test Team Already Linked",
            authentik_group="WCComps_BlueTeam10",
        )

        user = await User.objects.acreate(username="testuser")
        await DiscordLink.objects.acreate(
            discord_id=mock_interaction.user.id,
            discord_username="testuser#1234",
            user=user,
            is_active=True,
            team=team,
        )

        cog = LinkingCog(mock_bot)
        await cog.link_command.callback(cog, mock_interaction)

        # Verify rejection message
        call_args = mock_interaction.followup.send.call_args
        assert "already linked" in call_args.args[0].lower()

    async def test_link_command_rate_limiting(self, mock_interaction: Any, mock_bot: Any) -> None:
        """Test that /link command enforces rate limiting."""
        # Create 5 recent link attempts (limit is 5 per hour)
        for _ in range(5):
            await LinkRateLimit.objects.acreate(discord_id=mock_interaction.user.id)

        cog = LinkingCog(mock_bot)
        await cog.link_command.callback(cog, mock_interaction)

        # Verify rate limit rejection
        call_args = mock_interaction.followup.send.call_args
        assert "rate limit" in call_args.args[0].lower()

    async def test_link_command_with_staff_link(self, mock_interaction: Any, mock_bot: Any) -> None:
        """A staff member (linked without a team) may run /link again."""
        discord_id = mock_interaction.user.id
        user = await User.objects.acreate(username="olduser")
        await DiscordLink.objects.acreate(
            discord_id=discord_id,
            discord_username="olduser#1234",
            user=user,
            is_active=True,
            team=None,
        )

        cog = LinkingCog(mock_bot)

        with patch("bot.cogs.linking.settings") as mock_settings:
            mock_settings.BASE_URL = "https://test.example.com"
            await cog.link_command.callback(cog, mock_interaction)

        # Verify new token was created (the staff link didn't block a new link attempt)
        token = await LinkToken.objects.filter(discord_id=discord_id).afirst()
        assert token is not None

        # Verify response was sent
        mock_interaction.followup.send.assert_called_once()

        # The staff link stays active until the new link replaces it.
        assert await DiscordLink.objects.filter(discord_id=discord_id, is_active=True).aexists()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
class TestLinkTokenValidation:
    """Test link token validation."""

    async def test_token_expiration_check(self) -> None:
        """Test that expired tokens are detected."""
        expired_token = await LinkToken.objects.acreate(
            token="expired_token_123",
            discord_id=111111111,
            discord_username="testuser",
            expires_at=timezone.now() - timedelta(minutes=1),
        )

        assert expired_token.is_expired()

    async def test_valid_token_not_expired(self) -> None:
        """Test that valid tokens are not marked as expired."""
        valid_token = await LinkToken.objects.acreate(
            token="valid_token_123",
            discord_id=111111111,
            discord_username="testuser",
            expires_at=timezone.now() + timedelta(minutes=15),
        )

        assert not valid_token.is_expired()

    async def test_used_token_cannot_be_reused(self) -> None:
        """Test that used tokens cannot be used again."""
        token = await LinkToken.objects.acreate(
            token="used_token_123",
            discord_id=111111111,
            discord_username="testuser",
            used=True,
            expires_at=timezone.now() + timedelta(minutes=15),
        )

        # Attempting to get unused token should fail
        unused_token = await LinkToken.objects.filter(token=token.token, used=False).afirst()
        assert unused_token is None


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
class TestDiscordLinkCreation:
    """Test DiscordLink creation during OAuth callback."""

    async def test_create_link_for_team_member(self) -> None:
        """Test creating DiscordLink for team member."""
        team = await Team.objects.acreate(
            team_number=11,
            team_name="Test Team Link Creation",
        )

        user = await User.objects.acreate(username="team61_member1")
        link = await DiscordLink.objects.acreate(
            discord_id=333333333,
            discord_username="team61_member1#0001",
            user=user,
            is_active=True,
            team=team,
        )

        assert link.team == team
        assert link.is_active
        assert link.unlinked_at is None

    async def test_prevent_duplicate_active_links(self) -> None:
        """Test that duplicate active links are prevented."""
        team = await Team.objects.acreate(
            team_number=12,
            team_name="Test Team Duplicate",
        )

        # Create first link
        user = await User.objects.acreate(username="team62_member1")
        await DiscordLink.objects.acreate(
            discord_id=444444444,
            discord_username="team62_member1#0001",
            user=user,
            is_active=True,
            team=team,
        )

        # Check that attempting to link same Discord ID again finds existing link
        existing_link = (
            await DiscordLink.objects.filter(discord_id=444444444, is_active=True).select_related("team").afirst()
        )
        assert existing_link is not None
        assert existing_link.team == team

    async def test_deactivate_link(self) -> None:
        """Test deactivating a DiscordLink."""
        team = await Team.objects.acreate(
            team_number=13,
            team_name="Test Team Deactivate",
        )

        user = await User.objects.acreate(username="team63_member1")
        link = await DiscordLink.objects.acreate(
            discord_id=555555555,
            discord_username="team63_member1#0001",
            user=user,
            is_active=True,
            team=team,
        )

        # Deactivate the link
        link.is_active = False
        link.unlinked_at = timezone.now()
        await link.asave()

        await link.arefresh_from_db()
        assert not link.is_active
        assert link.unlinked_at is not None


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
class TestRateLimitCleanup:
    """Test rate limit cleanup."""

    async def test_rate_limit_old_attempts_not_counted(self) -> None:
        """Test that old rate limit attempts don't count towards limit."""
        discord_id = 888888888

        # Create old attempt (2 hours ago, outside 1 hour window)
        old_attempt = await LinkRateLimit.objects.acreate(discord_id=discord_id)
        old_attempt.attempted_at = timezone.now() - timedelta(hours=2)
        await old_attempt.asave()

        # Count recent attempts (within last hour)
        one_hour_ago = timezone.now() - timedelta(hours=1)
        recent_count = await LinkRateLimit.objects.filter(
            discord_id=discord_id, attempted_at__gte=one_hour_ago
        ).acount()

        # Old attempt should not be counted
        assert recent_count == 0
