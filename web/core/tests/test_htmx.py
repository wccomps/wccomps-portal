"""Tests for htmx partial responses in core views."""

from unittest.mock import MagicMock, patch

import pytest
from django.test import Client
from django.urls import reverse

from team.models import DiscordLink

pytestmark = pytest.mark.django_db


class TestCreateTicketTemplate:
    """Tests for create_ticket template rendering."""

    @pytest.fixture
    def mock_quotient(self):
        """Mock Quotient client to avoid API calls."""
        mock_client = MagicMock()
        mock_client.get_infrastructure.return_value = None
        mock_client.get_service_choices.return_value = [
            {
                "value": "web:http",
                "label": "web - HTTP",
                "box_ip": "10.0.0.1",
                "box_name": "web",
                "service_name": "http",
            }
        ]
        mock_client.get_box_names.return_value = ["web", "mail"]

        with patch("quotient.client.get_quotient_client", return_value=mock_client):
            yield mock_client

    def test_create_ticket_renders_service_dropdown_without_template_tag(
        self, blue_team_user, mock_quotient, blue_team
    ):
        """
        Service dropdown should NOT use <template x-for> inside <select>.

        This is a regression test for the Alpine.js bug where <template> elements
        inside <select> don't work in browsers.
        """
        team = blue_team
        DiscordLink.objects.create(
            discord_id=123456789,
            discord_username="blueteam01",
            user=blue_team_user,
            team=team,
            is_active=True,
        )

        client = Client()
        client.force_login(blue_team_user)
        response = client.get(reverse("create_ticket"))

        assert response.status_code == 200
        content = response.content.decode()

        # Verify the service dropdown exists
        assert 'name="service_name"' in content

        # The critical regression test: verify NO <template x-for> inside select
        # This pattern doesn't work in browsers - Alpine can't process templates inside select
        assert "<template x-for" not in content

        # Verify Alpine.js setup is present (rebuildServiceOptions method)
        assert "rebuildServiceOptions" in content

    def test_create_ticket_passes_categories_to_template(self, blue_team_user, mock_quotient, blue_team):
        """Create ticket view should pass categories to template."""
        team = blue_team
        DiscordLink.objects.create(
            discord_id=123456789,
            discord_username="blueteam01",
            user=blue_team_user,
            team=team,
            is_active=True,
        )

        client = Client()
        client.force_login(blue_team_user)
        response = client.get(reverse("create_ticket"))

        assert response.status_code == 200
        content = response.content.decode()

        # Verify categories are in the dropdown (by display name)
        assert "Scoring Service Check" in content
        assert "Box Reset / Scrub" in content


@pytest.fixture
def team_with_link(blue_team_user, blue_team):
    """Create a team with DiscordLink for blue_team_user."""
    team = blue_team
    DiscordLink.objects.create(
        discord_id=123456789,
        discord_username="blueteam01",
        user=blue_team_user,
        team=team,
        is_active=True,
    )
    return blue_team_user


class TestOpsTicketListHtmx:
    """Tests for ops_ticket_list htmx partial responses."""

    def test_htmx_request_returns_partial(self, ticketing_support_user):
        """htmx request returns only the ticket list table partial."""
        client = Client()
        client.force_login(ticketing_support_user)
        response = client.get(
            reverse("ticket_list") + "?status=all",
            HTTP_HX_REQUEST="true",
        )
        assert response.status_code == 200
        content = response.content.decode()
        assert 'id="ticket-list-content"' in content
        assert "<title>" not in content

    def test_regular_request_returns_full_page(self, ticketing_support_user):
        """Regular request returns full page."""
        client = Client()
        client.force_login(ticketing_support_user)
        response = client.get(reverse("ticket_list"))
        assert response.status_code == 200
        content = response.content.decode()
        assert "<title>" in content
        assert "Support Tickets" in content


class TestTeamTicketsHtmx:
    """Tests for team_tickets htmx partial responses."""

    def test_htmx_request_returns_partial(self, team_with_link):
        """htmx request returns only the ticket list partial."""
        client = Client()
        client.force_login(team_with_link)
        response = client.get(
            reverse("ticket_list") + "?status=all",
            HTTP_HX_REQUEST="true",
        )
        assert response.status_code == 200
        content = response.content.decode()
        assert 'id="ticket-list-content"' in content
        assert "<title>" not in content

    def test_regular_request_returns_full_page(self, team_with_link):
        """Regular request returns full page."""
        client = Client()
        client.force_login(team_with_link)
        response = client.get(reverse("ticket_list"))
        assert response.status_code == 200
        content = response.content.decode()
        assert "<title>" in content


class TestOpsReviewTicketsHtmx:
    """Tests for ops_review_tickets htmx partial responses."""

    def test_htmx_request_returns_partial(self, ticketing_admin_user):
        """htmx request returns only the review tickets table partial."""
        client = Client()
        client.force_login(ticketing_admin_user)
        response = client.get(
            reverse("ops_review_tickets") + "?verified=unverified",
            HTTP_HX_REQUEST="true",
        )
        assert response.status_code == 200
        content = response.content.decode()
        assert 'id="review-tickets-content"' in content
        assert "<title>" not in content

    def test_regular_request_returns_full_page(self, ticketing_admin_user):
        """Regular request returns full page."""
        client = Client()
        client.force_login(ticketing_admin_user)
        response = client.get(reverse("ops_review_tickets"))
        assert response.status_code == 200
        content = response.content.decode()
        assert "<title>" in content
        assert "Review Tickets" in content
