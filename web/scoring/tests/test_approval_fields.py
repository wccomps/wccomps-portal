"""
Tests for RedTeamScore approval fields (MODEL-1).

Uses parametrization for field default tests and focuses on behavior.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from django.contrib.auth.models import User

from scoring.models import RedTeamScore
from team.models import Team

pytestmark = pytest.mark.django_db


@pytest.fixture
def red_user():
    """Create a red team user."""
    return User.objects.create_user(username="redteam", password="test123")


@pytest.fixture
def gold_user():
    """Create a gold team user."""
    return User.objects.create_user(username="goldteam", password="test123")


@pytest.fixture
def team():
    """Create a test team."""
    return Team.objects.create(team_number=1, team_name="Team 1")


@pytest.fixture
def unapproved_finding(red_user):
    """Create an unapproved finding."""
    return RedTeamScore.objects.create(
        source_ip="10.0.0.5",
        points_per_team=Decimal("0"),
        submitted_by=red_user,
    )


class TestFindingDefaults:
    """Test RedTeamScore default values."""

    def test_new_finding_is_not_approved(self, unapproved_finding):
        """New findings should default to unapproved state."""
        assert unapproved_finding.is_approved is False
        assert unapproved_finding.approved_at is None
        assert unapproved_finding.approved_by is None


class TestFindingApprovalWorkflow:
    """Test RedTeamScore approval workflow."""

    def test_can_approve_finding(self, unapproved_finding, gold_user):
        """Should be able to approve a finding."""
        approval_time = datetime.now(UTC)
        unapproved_finding.is_approved = True
        unapproved_finding.approved_at = approval_time
        unapproved_finding.approved_by = gold_user
        unapproved_finding.points_per_team = Decimal("30.00")
        unapproved_finding.save()
        unapproved_finding.refresh_from_db()

        assert unapproved_finding.is_approved is True
        assert unapproved_finding.approved_at == approval_time
        assert unapproved_finding.approved_by == gold_user

    def test_approval_persists_after_approver_deleted(self, red_user, gold_user):
        """Finding should preserve is_approved when approver is deleted."""
        approver = User.objects.create_user(username="approver", password="test123")

        finding = RedTeamScore.objects.create(
            source_ip="10.0.0.5",
            points_per_team=Decimal("30.00"),
            submitted_by=red_user,
            is_approved=True,
            approved_by=approver,
            approved_at=datetime.now(UTC),
        )

        approver.delete()
        finding.refresh_from_db()

        assert finding.is_approved is True
        assert finding.approved_by is None
        assert RedTeamScore.objects.filter(pk=finding.pk).exists()


class TestFindingQueries:
    """Test RedTeamScore query capabilities."""

    def test_can_filter_by_approval_status(self, red_user, gold_user):
        """Should be able to filter findings by approval status."""
        RedTeamScore.objects.create(
            source_ip="10.0.0.5",
            points_per_team=Decimal("30.00"),
            submitted_by=red_user,
            is_approved=True,
            approved_by=gold_user,
            approved_at=datetime.now(UTC),
        )
        RedTeamScore.objects.create(
            source_ip="10.0.0.6",
            points_per_team=Decimal("0"),
            submitted_by=red_user,
        )

        assert RedTeamScore.objects.filter(is_approved=True).count() == 1
        assert RedTeamScore.objects.filter(is_approved=False).count() == 1

    def test_approval_works_with_all_model_fields(self, red_user, gold_user, team):
        """Approval fields should work with all other model fields."""
        finding = RedTeamScore.objects.create(
            source_ip="10.0.0.5",
            destination_ip_template="10.100.1X.22",
            affected_boxes=["web-server"],
            affected_service="HTTP",
            universally_attempted=True,
            persistence_established=True,
            points_per_team=Decimal("50.00"),
            notes="Test notes",
            submitted_by=red_user,
            is_approved=True,
            approved_by=gold_user,
            approved_at=datetime.now(UTC),
        )
        finding.affected_teams.add(team)

        assert finding.is_approved is True
        assert finding.affected_teams.count() == 1
