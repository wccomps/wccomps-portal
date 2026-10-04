"""Tests for Discord linking logic and uniqueness constraints."""

import uuid

import pytest
from django.contrib.auth.models import User

from core.models import CompetitionConfig
from team.models import DiscordLink, Team


def _set_member_limit(limit: int) -> None:
    CompetitionConfig.objects.update_or_create(pk=1, defaults={"max_team_members": limit})


def create_test_user(username: str | None = None) -> User:
    """Create a test user with unique username."""
    if username is None:
        username = f"test_user_{uuid.uuid4().hex[:8]}"
    return User.objects.create_user(username=username, password="testpass123")


@pytest.mark.django_db(transaction=True)
class TestDiscordLinkUniqueness:
    """Test uniqueness constraints for active DiscordLinks."""

    def test_multiple_inactive_links_allowed(self, db) -> None:
        """Test that multiple inactive links can exist for same discord_id."""
        discord_id = 987654321
        user1 = create_test_user()
        user2 = create_test_user()

        # Create first inactive link
        link1 = DiscordLink.objects.create(
            discord_id=discord_id,
            discord_username="user1",
            user=user1,
            is_active=False,
        )

        # Create second inactive link
        link2 = DiscordLink.objects.create(
            discord_id=discord_id,
            discord_username="user2",
            user=user2,
            is_active=False,
        )

        # Both should exist and be inactive
        assert not link1.is_active
        assert not link2.is_active

        # Verify count
        assert DiscordLink.objects.filter(discord_id=discord_id, is_active=False).count() == 2
        assert DiscordLink.objects.filter(discord_id=discord_id, is_active=True).count() == 0

    def test_inactive_links_dont_conflict_with_active(self, db) -> None:
        """Test creating active link doesn't affect existing inactive links."""
        discord_id = 555555555
        user1 = create_test_user()
        user2 = create_test_user()

        # Create inactive link (history)
        link1 = DiscordLink.objects.create(
            discord_id=discord_id,
            discord_username="old_user",
            user=user1,
            is_active=False,
        )

        # Create active link
        link2 = DiscordLink.objects.create(
            discord_id=discord_id,
            discord_username="current_user",
            user=user2,
            is_active=True,
        )

        # Refresh first to get latest state
        link1.refresh_from_db()

        # First should still be inactive
        assert not link1.is_active

        # Second should be active
        assert link2.is_active

        # Verify counts
        assert DiscordLink.objects.filter(discord_id=discord_id, is_active=False).count() == 1
        assert DiscordLink.objects.filter(discord_id=discord_id, is_active=True).count() == 1

    def test_creating_new_active_link_deactivates_old_history_preserved(self, db) -> None:
        """Test creating new active link deactivates old one, but keeps history."""
        discord_id = 444444444
        user1 = create_test_user()
        user2 = create_test_user()
        user3 = create_test_user()

        # Create first active link
        link1 = DiscordLink.objects.create(
            discord_id=discord_id,
            discord_username="user_v1",
            user=user1,
            is_active=True,
        )

        # Create second active link (deactivate previous first)
        DiscordLink.deactivate_previous_links(discord_id)
        link2 = DiscordLink.objects.create(
            discord_id=discord_id,
            discord_username="user_v2",
            user=user2,
            is_active=True,
        )

        # Create third active link (deactivate previous first)
        DiscordLink.deactivate_previous_links(discord_id)
        link3 = DiscordLink.objects.create(
            discord_id=discord_id,
            discord_username="user_v3",
            user=user3,
            is_active=True,
        )

        # Refresh to get latest state
        link1.refresh_from_db()
        link2.refresh_from_db()

        # Only link3 should be active
        assert not link1.is_active
        assert not link2.is_active
        assert link3.is_active

        # All three should exist in database (history preserved)
        all_links = DiscordLink.objects.filter(discord_id=discord_id).order_by("linked_at")
        assert all_links.count() == 3
        assert list(all_links.values_list("discord_username", flat=True)) == [
            "user_v1",
            "user_v2",
            "user_v3",
        ]


@pytest.mark.django_db(transaction=True)
class TestTeamMemberLimitEnforcement:
    """Test team member limits are enforced during linking."""

    def test_team_is_full_method_at_capacity(self, db) -> None:
        """Team.is_full() returns True when members equals max_members."""
        _set_member_limit(3)
        team = Team.objects.create(
            team_number=20,
            team_name="Full Team",
            authentik_group="WCComps_BlueTeam10",
        )

        # Create 3 active members
        for i in range(3):
            user = create_test_user()
            DiscordLink.objects.create(
                discord_id=1000000 + i,
                discord_username=f"user{i}",
                user=user,
                team=team,
                is_active=True,
            )

        assert team.get_member_count() == 3
        assert team.is_full() is True

    def test_team_is_full_method_below_capacity(self, db) -> None:
        """Team.is_full() returns False when members less than max_members."""
        team = Team.objects.create(
            team_number=21,
            team_name="Open Team",
            authentik_group="WCComps_BlueTeam11",
        )

        # Create 2 active members
        for i in range(2):
            user = create_test_user()
            DiscordLink.objects.create(
                discord_id=2000000 + i,
                discord_username=f"user{i}",
                user=user,
                team=team,
                is_active=True,
            )

        assert team.get_member_count() == 2
        assert team.is_full() is False

    def test_team_is_full_method_empty(self, db) -> None:
        """Team.is_full() returns False when team has no members."""
        team = Team.objects.create(
            team_number=22,
            team_name="Empty Team",
            authentik_group="WCComps_BlueTeam12",
        )

        assert team.get_member_count() == 0
        assert team.is_full() is False

    def test_get_member_count_counts_only_active(self, db) -> None:
        """get_member_count() counts only active members, not inactive ones."""
        from django.utils import timezone

        team = Team.objects.create(
            team_number=23,
            team_name="Mixed Activity",
            authentik_group="WCComps_BlueTeam13",
        )

        # Create 2 active members
        for i in range(2):
            user = create_test_user()
            DiscordLink.objects.create(
                discord_id=3000000 + i,
                discord_username=f"active{i}",
                user=user,
                team=team,
                is_active=True,
            )

        # Create 2 inactive members
        for i in range(2):
            user = create_test_user()
            DiscordLink.objects.create(
                discord_id=3000010 + i,
                discord_username=f"inactive{i}",
                user=user,
                team=team,
                is_active=False,
                unlinked_at=timezone.now(),
            )

        assert team.get_member_count() == 2

    def test_team_is_full_ignores_inactive_members(self, db) -> None:
        """Team.is_full() does not count inactive members toward capacity."""
        from django.utils import timezone

        _set_member_limit(2)
        team = Team.objects.create(
            team_number=24,
            team_name="Capacity Test",
            authentik_group="WCComps_BlueTeam14",
        )

        # Create 1 active member
        user = create_test_user()
        DiscordLink.objects.create(
            discord_id=4000000,
            discord_username="active",
            user=user,
            team=team,
            is_active=True,
        )

        # Create 5 inactive members (to verify they're not counted)
        for i in range(5):
            user = create_test_user()
            DiscordLink.objects.create(
                discord_id=4000010 + i,
                discord_username=f"inactive{i}",
                user=user,
                team=team,
                is_active=False,
                unlinked_at=timezone.now(),
            )

        assert team.get_member_count() == 1
        assert team.is_full() is False

    def test_team_full_error_message_includes_capacity(self, db) -> None:
        """LinkAttempt records full team with member count and capacity."""
        from team.models import LinkAttempt

        _set_member_limit(2)
        team = Team.objects.create(
            team_number=25,
            team_name="Full Test Team",
            authentik_group="WCComps_BlueTeam15",
        )

        for i in range(2):
            user = create_test_user()
            DiscordLink.objects.create(
                discord_id=5000000 + i,
                discord_username=f"user{i}",
                user=user,
                team=team,
                is_active=True,
            )

        # Create failed link attempt with full team error
        attempt = LinkAttempt.objects.create(
            discord_id=5000010,
            discord_username="denied",
            authentik_username="denied",
            team=team,
            success=False,
            failure_reason=f"Team full ({team.get_member_count()}/{team.max_members})",
        )

        assert "Team full" in attempt.failure_reason
        assert "2/2" in attempt.failure_reason

    def test_select_for_update_pattern_usage(self, db) -> None:
        """Test that select_for_update can be used with team capacity checks."""
        from django.db import transaction

        _set_member_limit(3)
        team = Team.objects.create(
            team_number=26,
            team_name="Locking Pattern Test",
            authentik_group="WCComps_BlueTeam26",
        )

        # Add 2 members
        for i in range(2):
            user = create_test_user()
            DiscordLink.objects.create(
                discord_id=10000000 + i,
                discord_username=f"existing{i}",
                user=user,
                team=team,
                is_active=True,
            )

        # Verify select_for_update pattern works for capacity checking
        with transaction.atomic():
            locked_team = Team.objects.select_for_update().get(pk=team.pk)
            assert locked_team.is_full() is False

            user = create_test_user()
            DiscordLink.objects.create(
                discord_id=10000010,
                discord_username="new_member",
                user=user,
                team=locked_team,
                is_active=True,
            )

        team.refresh_from_db()
        assert team.get_member_count() == 3


class TestLinkCommandDefers:
    """link_command defers immediately so its DB/token work can't exceed Discord's 3s window."""

    @pytest.mark.asyncio
    @pytest.mark.django_db(transaction=True)
    async def test_defers_then_uses_followup(self):
        from unittest.mock import AsyncMock, MagicMock, patch

        from bot.cogs import linking

        cog = linking.LinkingCog(MagicMock())
        interaction = MagicMock()
        interaction.user.id = 123
        interaction.response.defer = AsyncMock()
        interaction.response.send_message = AsyncMock()
        interaction.followup.send = AsyncMock()

        with (
            patch.object(linking, "check_rate_limit", AsyncMock(return_value=(True, 0))),
            patch.object(
                linking,
                "check_existing_link",
                AsyncMock(return_value=linking.LinkCheckResult(can_link=True, error_message="")),
            ),
            patch.object(linking, "create_link_token", AsyncMock(return_value="https://auth.example/link")),
        ):
            await linking.LinkingCog.link_command.callback(cog, interaction)

        interaction.response.defer.assert_awaited_once()
        interaction.followup.send.assert_awaited_once()
        interaction.response.send_message.assert_not_called()
