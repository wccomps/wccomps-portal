"""Tests for htmx partial responses in scoring views."""

from unittest.mock import MagicMock, patch

import pytest
from django.test import Client
from django.urls import reverse

from team.models import Team

pytestmark = pytest.mark.django_db


@pytest.fixture
def mock_quotient_injects():
    """Mock Quotient client with sample injects."""
    from quotient.client import Inject

    with patch("quotient.client.QuotientClient") as mock_client:
        mock_inject = Inject(inject_id=1, title="Test Inject", description="Test description")

        instance = MagicMock()
        instance.get_injects.return_value = [mock_inject]
        mock_client.return_value = instance
        yield instance


@pytest.fixture
def teams_for_grading():
    """Create teams for inject grading tests."""
    Team.objects.create(team_number=1, team_name="Team 1", is_active=True)
    Team.objects.create(team_number=2, team_name="Team 2", is_active=True)


class TestInjectGradingHtmx:
    """Tests for inject_grading htmx partial responses."""

    def test_htmx_request_returns_partial(self, gold_team_user, mock_quotient_injects, teams_for_grading):
        """htmx request returns only the grading content partial."""
        client = Client()
        client.force_login(gold_team_user)
        response = client.get(
            reverse("scoring:inject_grading") + "?inject=1",
            HTTP_HX_REQUEST="true",
        )
        assert response.status_code == 200
        content = response.content.decode()
        # Partial should contain the content wrapper
        assert 'id="inject-grading-content"' in content
        # Partial should NOT contain full page elements
        assert "<title>" not in content
        assert "c-module" not in content

    def test_regular_request_returns_full_page(self, gold_team_user, mock_quotient_injects, teams_for_grading):
        """Regular request returns full page with navigation."""
        client = Client()
        client.force_login(gold_team_user)
        response = client.get(reverse("scoring:inject_grading"))
        assert response.status_code == 200
        content = response.content.decode()
        # Full page should contain page title and navigation
        assert "<title>" in content
        assert "Inject Grading" in content


class TestRedTeamFindingsHtmx:
    """Tests for red_team_findings htmx partial responses."""

    def test_htmx_request_returns_partial(self, gold_team_user):
        """htmx request returns only the findings table partial."""
        client = Client()
        client.force_login(gold_team_user)
        response = client.get(
            reverse("scoring:red_team_findings") + "?status=pending",
            HTTP_HX_REQUEST="true",
        )
        assert response.status_code == 200
        content = response.content.decode()
        # Partial should contain the content wrapper
        assert 'id="red-findings-content"' in content
        # Partial should NOT contain full page elements
        assert "<title>" not in content
        assert "c-filter_toolbar" not in content

    def test_regular_request_returns_full_page(self, gold_team_user):
        """Regular request returns full page with filter toolbar."""
        client = Client()
        client.force_login(gold_team_user)
        response = client.get(reverse("scoring:red_team_findings"))
        assert response.status_code == 200
        content = response.content.decode()
        # Full page should contain filter toolbar
        assert "changelist-filter" in content


class TestReviewIncidentsHtmx:
    """Tests for review_incidents htmx partial responses."""

    def test_htmx_request_returns_partial(self, gold_team_user):
        """htmx request returns only the incidents table partial."""
        client = Client()
        client.force_login(gold_team_user)
        response = client.get(
            reverse("scoring:review_incidents") + "?status=pending",
            HTTP_HX_REQUEST="true",
        )
        assert response.status_code == 200
        content = response.content.decode()
        assert 'id="review-incidents-content"' in content
        assert "<title>" not in content

    def test_regular_request_returns_full_page(self, gold_team_user):
        """Regular request returns full page."""
        client = Client()
        client.force_login(gold_team_user)
        response = client.get(reverse("scoring:review_incidents"))
        assert response.status_code == 200
        content = response.content.decode()
        assert "<title>" in content
        assert "Review Incident Reports" in content


class TestInjectGradesReviewHtmx:
    """Tests for inject_grades_review htmx partial responses."""

    def test_htmx_request_returns_partial(self, gold_team_user):
        """htmx request returns only the grades table partial."""
        client = Client()
        client.force_login(gold_team_user)
        response = client.get(
            reverse("scoring:inject_grades_review") + "?status=pending",
            HTTP_HX_REQUEST="true",
        )
        assert response.status_code == 200
        content = response.content.decode()
        assert 'id="inject-grades-content"' in content
        assert "<title>" not in content

    def test_regular_request_returns_full_page(self, gold_team_user):
        """Regular request returns full page."""
        client = Client()
        client.force_login(gold_team_user)
        response = client.get(reverse("scoring:inject_grades_review"))
        assert response.status_code == 200
        content = response.content.decode()
        assert "<title>" in content
        assert "Review Inject Grades" in content
