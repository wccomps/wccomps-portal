"""Scoring workflow integration tests.

Tests cross-role scoring flows through Django views.
"""

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from scoring.models import IncidentReport
from team.models import Team

pytestmark = pytest.mark.django_db


@pytest.fixture
def team(db):
    """Create a team for scoring tests."""
    return Team.objects.create(team_number=1, team_name="Test Team", authentik_group="WCComps_BlueTeam01")


class TestIncidentReportFlow:
    """Blue team submits incident -> gold team reviews."""

    def test_gold_team_can_access_incident_review(self, gold_team_user, mock_quotient_client):
        client = Client()
        client.force_login(gold_team_user)
        response = client.get(reverse("scoring:review_incidents"))
        assert response.status_code == 200

    def test_blue_team_cannot_access_gold_review(self, blue_team_user, mock_quotient_client):
        client = Client()
        client.force_login(blue_team_user)
        response = client.get(reverse("scoring:review_incidents"))
        assert response.status_code == 302

    def test_incident_appears_in_gold_review(self, team, blue_team_user, gold_team_user, mock_quotient_client):
        """Blue team creates incident, gold team sees it in review."""
        # Create an incident report
        incident = IncidentReport.objects.create(
            team=team,
            submitted_by=blue_team_user,
            attack_description="SQL injection attempt",
            source_ip="10.0.0.1",
            destination_ip="10.100.11.22",
            attack_detected_at=timezone.now(),
            attack_mitigated=True,
            evidence_notes="Blocked via WAF",
        )

        # Gold team views review page
        client = Client()
        client.force_login(gold_team_user)
        response = client.get(reverse("scoring:review_incidents"))
        assert response.status_code == 200
        assert incident.attack_description in response.content.decode()


class TestRedTeamFindingFlow:
    """Red team submits finding -> gold team sees in findings page."""

    def test_red_team_can_access_findings_view(self, red_team_user, mock_quotient_client):
        client = Client()
        client.force_login(red_team_user)
        response = client.get(reverse("scoring:red_team_scores"))
        assert response.status_code == 200

    def test_gold_team_can_access_red_team_findings(self, gold_team_user, mock_quotient_client):
        client = Client()
        client.force_login(gold_team_user)
        response = client.get(reverse("scoring:red_team_findings"))
        assert response.status_code == 200

    def test_blue_team_cannot_access_red_findings(self, blue_team_user, mock_quotient_client):
        client = Client()
        client.force_login(blue_team_user)
        response = client.get(reverse("scoring:red_team_findings"))
        assert response.status_code == 302


class TestInjectGradingFlow:
    """White team and gold team can access inject grading."""

    def test_white_team_can_access_inject_grading(self, white_team_user, mock_quotient_client):
        client = Client()
        client.force_login(white_team_user)
        response = client.get(reverse("scoring:inject_grading"))
        assert response.status_code == 200

    def test_gold_team_can_access_inject_grading(self, gold_team_user, mock_quotient_client):
        client = Client()
        client.force_login(gold_team_user)
        response = client.get(reverse("scoring:inject_grading"))
        assert response.status_code == 200

    def test_red_team_cannot_access_inject_grading(self, red_team_user, mock_quotient_client):
        client = Client()
        client.force_login(red_team_user)
        response = client.get(reverse("scoring:inject_grading"))
        assert response.status_code == 302
