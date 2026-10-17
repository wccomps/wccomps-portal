import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from core.models import UserGroups
from orange_team.models import (
    OrangeAssignment,
    OrangeAssignmentResult,
    OrangeCheck,
    OrangeCheckCriterion,
)
from team.models import Team

pytestmark = pytest.mark.django_db


class TestAssignmentApprove:
    def test_approve_creates_score(self) -> None:
        lead = User.objects.create_user(username="gold1", password="test")
        UserGroups.objects.create(user=lead, authentik_id="g1", groups=["WCComps_GoldTeam"])
        orange = User.objects.create_user(username="orange1")
        team = Team.objects.create(team_number=1, team_name="T1")
        check = OrangeCheck.objects.create(title="Test", description="Desc")
        c1 = OrangeCheckCriterion.objects.create(orange_check=check, label="C1", points=5)
        assignment = OrangeAssignment.objects.create(
            orange_check=check, user=orange, team=team, status="submitted", score=5
        )
        OrangeAssignmentResult.objects.create(assignment=assignment, criterion=c1, met=True)
        client = Client()
        client.force_login(User.objects.get(username="gold1"))
        response = client.post(f"/orange-team/assignments/{assignment.pk}/approve/")
        assert response.status_code == 302
        assignment.refresh_from_db()
        assert assignment.status == "approved"
        assert assignment.reviewed_by == lead
        from scoring.models import OrangeTeamScore

        score = OrangeTeamScore.objects.get(team=team)
        assert score.points_awarded == 5
        assert score.is_approved

    def test_non_lead_cannot_approve(self) -> None:
        user = User.objects.create_user(username="orange1", password="test")
        UserGroups.objects.create(user=user, authentik_id="o1", groups=["WCComps_OrangeTeam"])
        team = Team.objects.create(team_number=1, team_name="T1")
        check = OrangeCheck.objects.create(title="Test", description="Desc")
        assignment = OrangeAssignment.objects.create(
            orange_check=check, user=user, team=team, status="submitted", score=5
        )
        client = Client()
        client.force_login(User.objects.get(username="orange1"))
        response = client.post(f"/orange-team/assignments/{assignment.pk}/approve/")
        assert response.status_code == 302  # redirected by require_permission
        assignment.refresh_from_db()
        assert assignment.status == "submitted"  # unchanged


class TestAssignmentReject:
    def test_reject_with_notes(self) -> None:
        lead = User.objects.create_user(username="gold1", password="test")
        UserGroups.objects.create(user=lead, authentik_id="g1", groups=["WCComps_GoldTeam"])
        orange = User.objects.create_user(username="orange1")
        team = Team.objects.create(team_number=1, team_name="T1")
        check = OrangeCheck.objects.create(title="Test", description="Desc")
        assignment = OrangeAssignment.objects.create(
            orange_check=check, user=orange, team=team, status="submitted", score=5
        )
        client = Client()
        client.force_login(User.objects.get(username="gold1"))
        response = client.post(
            f"/orange-team/assignments/{assignment.pk}/reject/",
            {"notes": "Please re-check team 1"},
        )
        assert response.status_code == 302
        assignment.refresh_from_db()
        assert assignment.status == "rejected"
        assert assignment.notes == "Please re-check team 1"


class TestExportScores:
    def test_export_csv(self) -> None:
        lead = User.objects.create_user(username="gold1", password="test")
        UserGroups.objects.create(user=lead, authentik_id="g1", groups=["WCComps_GoldTeam"])
        orange = User.objects.create_user(username="orange1")
        team = Team.objects.create(team_number=1, team_name="T1")
        check = OrangeCheck.objects.create(title="Test", description="Desc")
        OrangeAssignment.objects.create(
            orange_check=check,
            user=orange,
            team=team,
            status="submitted",
            score=5,
            submitted_at=timezone.now(),
        )
        client = Client()
        client.force_login(User.objects.get(username="gold1"))
        response = client.get("/orange-team/export/")
        assert response.status_code == 200
        assert response["Content-Type"] == "text/csv"
        content = response.content.decode()
        assert "Test" in content
        assert "orange1" in content
