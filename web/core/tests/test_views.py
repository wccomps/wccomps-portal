"""Unit tests for core/views.py."""

import pytest
from django.test import Client
from django.urls import reverse

from team.models import Team
from ticketing.models import Ticket, TicketCategory

pytestmark = pytest.mark.django_db


@pytest.fixture
def team1(blue_team):
    """Create team 1 (matches blue_team_user's group)."""
    return blue_team


@pytest.fixture
def team2():
    """Create team 2 (matches blue_team_02_user's group)."""
    return Team.objects.create(team_number=2, team_name="Team 02", is_active=True)


class TestHomeView:
    """Tests for home view redirect logic."""

    def test_blue_team_user_redirected_to_team_tickets(self, blue_team_user, team1):
        """Blue team users should be redirected to team tickets page."""
        client = Client()
        client.force_login(blue_team_user)

        response = client.get(reverse("home"))

        assert response.status_code == 302
        assert response.url == reverse("ticket_list")

    def test_admin_user_redirected_to_ops_tickets(self, admin_user):
        """Admin users without team should be redirected to ops ticket list."""
        client = Client()
        client.force_login(admin_user)

        response = client.get(reverse("home"))

        assert response.status_code == 302
        assert response.url == reverse("ticket_list")

    def test_ticketing_support_redirected_to_ops_tickets(self, ticketing_support_user):
        """Ticketing support users should be redirected to ops ticket list."""
        client = Client()
        client.force_login(ticketing_support_user)

        response = client.get(reverse("home"))

        assert response.status_code == 302
        assert response.url == reverse("ticket_list")

    def test_white_team_lands_on_leaderboard(self, white_team_user):
        client = Client()
        client.force_login(white_team_user)

        response = client.get(reverse("home"))

        assert response.status_code == 302
        assert response.url == reverse("leaderboard_page")

    def test_user_without_role_gets_403_not_a_redirect_loop(self, create_user_with_groups):
        client = Client()
        client.force_login(create_user_with_groups("injects_only", ["WCComps_Quotient_Injects"]))

        response = client.get(reverse("home"))

        assert response.status_code == 403
        assert b"no portal role" in response.content


class TestTeamTicketsView:
    """Tests for team_tickets view."""

    def test_blue_team_user_sees_their_tickets(self, blue_team_user, team1):
        """Blue team user should see tickets for their team."""
        Ticket.objects.create(
            team=team1,
            ticket_number="T001",
            category=TicketCategory.objects.get(pk=6),
            title="Test Ticket 1",
            status="open",
        )
        client = Client()
        client.force_login(blue_team_user)

        response = client.get(reverse("ticket_list"))

        assert response.status_code == 200
        # Template shows ticket number, not title
        assert b"T001" in response.content

    def test_status_filter_works(self, blue_team_user, team1):
        """Status filter should filter tickets."""
        Ticket.objects.create(
            team=team1,
            ticket_number="T001",
            category=TicketCategory.objects.get(pk=6),
            title="Open Ticket",
            status="open",
        )
        Ticket.objects.create(
            team=team1,
            ticket_number="T002",
            category=TicketCategory.objects.get(pk=2),
            title="Resolved Ticket",
            status="resolved",
        )
        client = Client()
        client.force_login(blue_team_user)

        response = client.get(reverse("ticket_list") + "?status=resolved")

        assert response.status_code == 200
        # Template shows ticket numbers
        assert b"T002" in response.content

    def test_support_user_without_team_sees_ops_view(self, ticketing_support_user):
        """Support users without a team see the ops ticket list."""
        client = Client()
        client.force_login(ticketing_support_user)

        response = client.get(reverse("ticket_list"))

        # Unified view shows ops ticket list for support users
        assert response.status_code == 200

    def test_admin_without_team_sees_ops_view(self, admin_user):
        """Admins without a team see the ops ticket list."""
        client = Client()
        client.force_login(admin_user)

        response = client.get(reverse("ticket_list"))

        # Unified view shows ops ticket list for admin users
        assert response.status_code == 200


class TestTicketDetailView:
    """Tests for ticket_detail view."""

    def test_team_member_can_view_their_ticket(self, blue_team_user, team1):
        """Team member should be able to view their team's ticket."""
        ticket = Ticket.objects.create(
            team=team1,
            ticket_number="T001",
            category=TicketCategory.objects.get(pk=6),
            title="Test Ticket",
            description="Test description",
            status="open",
        )
        client = Client()
        client.force_login(blue_team_user)

        response = client.get(reverse("ticket_detail", args=[ticket.ticket_number]))

        assert response.status_code == 200
        # Template shows ticket number in title
        assert b"T001" in response.content

    def test_other_team_cannot_view_ticket(self, blue_team_02_user, team1, team2):
        """Member of different team should not see the ticket."""
        ticket = Ticket.objects.create(
            team=team1,
            ticket_number="T001",
            category=TicketCategory.objects.get(pk=6),
            title="Test Ticket",
            status="open",
        )
        client = Client()
        client.force_login(blue_team_02_user)

        response = client.get(reverse("ticket_detail", args=[ticket.ticket_number]))

        assert response.status_code == 404

    def test_nonexistent_ticket_shows_error(self, blue_team_user, team1):
        """Accessing nonexistent ticket should show error."""
        client = Client()
        client.force_login(blue_team_user)

        response = client.get(reverse("ticket_detail", args=["T999-999"]))

        assert response.status_code == 404
        assert "error.html" in [t.name for t in response.templates]


class TestTicketCommentView:
    """Tests for ticket_comment view."""

    def test_get_request_not_allowed(self, blue_team_user, team1):
        """GET requests should return 405."""
        ticket = Ticket.objects.create(
            team=team1,
            ticket_number="T001",
            category=TicketCategory.objects.get(pk=6),
            title="Test Ticket",
            status="open",
        )
        client = Client()
        client.force_login(blue_team_user)

        response = client.get(reverse("ticket_comment", args=[ticket.ticket_number]))

        assert response.status_code == 405

    def test_empty_comment_rejected(self, blue_team_user, team1):
        """Empty comments should be rejected."""
        ticket = Ticket.objects.create(
            team=team1,
            ticket_number="T001",
            category=TicketCategory.objects.get(pk=6),
            title="Test Ticket",
            status="open",
        )
        client = Client()
        client.force_login(blue_team_user)

        response = client.post(reverse("ticket_comment", args=[ticket.ticket_number]), {"comment": ""})

        assert response.status_code == 302  # Redirect back with error message

    def test_valid_comment_created(self, blue_team_user, team1):
        """Valid comments should be created."""
        ticket = Ticket.objects.create(
            team=team1,
            ticket_number="T001",
            category=TicketCategory.objects.get(pk=6),
            title="Test Ticket",
            status="open",
        )
        client = Client()
        client.force_login(blue_team_user)

        response = client.post(
            reverse("ticket_comment", args=[ticket.ticket_number]),
            {"comment": "This is a test comment"},
        )

        assert response.status_code == 302
        assert ticket.comments.filter(comment_text="This is a test comment").exists()

    def test_other_team_cannot_comment(self, blue_team_02_user, team1, team2):
        """Member of different team should not be able to comment."""
        ticket = Ticket.objects.create(
            team=team1,
            ticket_number="T001",
            category=TicketCategory.objects.get(pk=6),
            title="Test Ticket",
            status="open",
        )
        client = Client()
        client.force_login(blue_team_02_user)

        response = client.post(
            reverse("ticket_comment", args=[ticket.ticket_number]),
            {"comment": "Unauthorized comment"},
        )

        assert response.status_code in (403, 404)


class TestCreateTicketView:
    """Tests for create_ticket view."""

    def test_team_member_sees_form(self, blue_team_user, team1):
        """Team member should see the ticket creation form."""
        client = Client()
        client.force_login(blue_team_user)

        response = client.get(reverse("create_ticket"))

        assert response.status_code == 200
        assert b"category" in response.content.lower()

    def test_admin_sees_team_selector(self, admin_user, team1):
        """Admin should see team selector in the form."""
        client = Client()
        client.force_login(admin_user)

        response = client.get(reverse("create_ticket"))

        assert response.status_code == 200

    def test_invalid_category_rejected(self, blue_team_user, team1):
        """Invalid category should be rejected."""
        client = Client()
        client.force_login(blue_team_user)

        response = client.post(
            reverse("create_ticket"),
            {
                "category": "invalid-category",
                "title": "Test",
                "description": "Test",
            },
        )

        assert response.status_code == 200
        assert b"Invalid" in response.content or b"invalid" in response.content


class TestOpsTicketListView:
    """Tests for ops_ticket_list view."""

    def test_ticketing_support_can_access(self, ticketing_support_user):
        """Ticketing support should be able to access ops ticket list."""
        client = Client()
        client.force_login(ticketing_support_user)

        response = client.get(reverse("ticket_list"))

        assert response.status_code == 200

    def test_blue_team_sees_their_tickets(self, blue_team_user, team1):
        """Blue team members see their own tickets via unified ticket_list."""
        client = Client()
        client.force_login(blue_team_user)

        response = client.get(reverse("ticket_list"))

        # Unified view shows team tickets for blue team members
        assert response.status_code == 200


class TestOpsTicketClaimView:
    """Tests for ops_ticket_claim view."""

    def test_support_can_claim_ticket(self, ticketing_support_user, team1):
        """Ticketing support should be able to claim an open ticket."""
        ticket = Ticket.objects.create(
            team=team1,
            ticket_number="T001",
            category=TicketCategory.objects.get(pk=6),
            title="Test Ticket",
            status="open",
        )
        client = Client()
        client.force_login(ticketing_support_user)

        response = client.post(reverse("ticket_claim", args=[ticket.ticket_number]))

        assert response.status_code == 302
        ticket.refresh_from_db()
        assert ticket.status == "claimed"

    def test_blue_team_gets_forbidden(self, blue_team_user, team1):
        """Blue team should get 403 when trying to claim."""
        ticket = Ticket.objects.create(
            team=team1,
            ticket_number="T001",
            category=TicketCategory.objects.get(pk=6),
            title="Test Ticket",
            status="open",
        )
        client = Client()
        client.force_login(blue_team_user)

        response = client.post(reverse("ticket_claim", args=[ticket.ticket_number]))

        assert response.status_code == 403
        ticket.refresh_from_db()
        assert ticket.status == "open"


class TestHealthCheckView:
    """Tests for health_check view."""

    def test_health_check_returns_200(self):
        """Health check should return 200 when database is healthy."""
        client = Client()

        response = client.get("/health/")

        assert response.status_code == 200

    def test_health_check_returns_json(self):
        """Health check should return JSON response."""
        client = Client()

        response = client.get("/health/")

        assert response["Content-Type"] == "application/json"

    def test_health_check_no_auth_required(self):
        """Health check should not require authentication."""
        client = Client()

        response = client.get("/health/")

        # Should not redirect to login
        assert response.status_code == 200

    def test_database_failure_is_503_without_its_detail(self, monkeypatch, caplog):
        from django.db import connection

        def broken_cursor(*args, **kwargs):
            raise RuntimeError('connection to server at "db-host.internal", role "secret_role" failed')

        monkeypatch.setattr(connection, "cursor", broken_cursor)
        response = Client().get("/health/")

        assert response.status_code == 503
        assert response.json() == {"status": "unhealthy"}
        assert "db-host.internal" in caplog.text

    def test_model_query_failure_is_503_without_its_detail(self, monkeypatch, caplog):
        from django.db.models import QuerySet

        def broken_exists(self):
            raise RuntimeError('relation "core_secret_table" does not exist')

        monkeypatch.setattr(QuerySet, "exists", broken_exists)
        response = Client().get("/health/")

        assert response.status_code == 503
        assert response.json() == {"status": "unhealthy"}
        assert "core_secret_table" in caplog.text

    def test_an_outage_logs_one_error_per_probe(self, monkeypatch, caplog):
        from django.db.models import QuerySet

        calls = []

        def broken_exists(self):
            calls.append(self.model)
            raise RuntimeError("connection refused")

        monkeypatch.setattr(QuerySet, "exists", broken_exists)
        Client().get("/health/")

        assert len(calls) == 1
        assert len([r for r in caplog.records if r.name == "core.views"]) == 1


class TestLinkInitiateView:
    """Tests for link_initiate view."""

    def test_missing_token_returns_400(self):
        """Missing token parameter should return 400."""
        client = Client()

        response = client.get("/auth/link")

        assert response.status_code == 400

    def test_invalid_token_shows_error(self):
        """Invalid token should show error page."""
        client = Client()

        response = client.get("/auth/link?token=invalid-token-12345")

        assert response.status_code == 200
        assert b"Invalid" in response.content or b"expired" in response.content


class TestAttachmentViews:
    """Tests for attachment upload/download views."""

    def test_upload_requires_file(self, blue_team_user, team1):
        """Upload without file should fail."""
        ticket = Ticket.objects.create(
            team=team1,
            ticket_number="T001",
            category=TicketCategory.objects.get(pk=6),
            title="Test Ticket",
            status="open",
        )
        client = Client()
        client.force_login(blue_team_user)

        response = client.post(reverse("ticket_attachment_upload", args=[ticket.ticket_number]))

        assert response.status_code == 400

    def test_download_nonexistent_attachment_fails(self, blue_team_user, team1):
        """Downloading nonexistent attachment should return 404."""
        ticket = Ticket.objects.create(
            team=team1,
            ticket_number="T001",
            category=TicketCategory.objects.get(pk=6),
            title="Test Ticket",
            status="open",
        )
        client = Client()
        client.force_login(blue_team_user)

        response = client.get(reverse("ticket_attachment_download", args=[ticket.ticket_number, 99999]))

        assert response.status_code == 404


class TestTicketNotifications:
    """Tests for ops_ticket_notifications endpoint."""

    @pytest.fixture
    def team1(self, blue_team):
        return blue_team

    def test_returns_open_count(self, ticketing_support_user, team1):
        """Should return count of open tickets."""
        Ticket.objects.create(
            team=team1, ticket_number="T001", category=TicketCategory.objects.get(pk=6), title="A", status="open"
        )
        Ticket.objects.create(
            team=team1, ticket_number="T002", category=TicketCategory.objects.get(pk=6), title="B", status="resolved"
        )
        client = Client()
        client.force_login(ticketing_support_user)

        response = client.get(reverse("ticket_notifications"))
        data = response.json()

        assert response.status_code == 200
        assert data["open_count"] == 1

    def test_returns_new_tickets_since_id(self, ticketing_support_user, team1):
        """Should return tickets with id > since_id."""
        t1 = Ticket.objects.create(
            team=team1, ticket_number="T001", category=TicketCategory.objects.get(pk=6), title="Old", status="open"
        )
        t2 = Ticket.objects.create(
            team=team1, ticket_number="T002", category=TicketCategory.objects.get(pk=2), title="New", status="open"
        )
        client = Client()
        client.force_login(ticketing_support_user)

        response = client.get(reverse("ticket_notifications") + f"?since_id={t1.id}")
        data = response.json()

        assert len(data["new_tickets"]) == 1
        assert data["new_tickets"][0]["id"] == t2.id
        assert data["new_tickets"][0]["number"] == "T002"
        assert data["new_tickets"][0]["category_display"] == "Box Reset / Scrub"

    def test_category_display_fallback(self, ticketing_support_user, team1):
        """Null category should fall back to 'Unknown'."""
        Ticket.objects.create(team=team1, ticket_number="T001", category=None, title="X", status="open")
        client = Client()
        client.force_login(ticketing_support_user)

        response = client.get(reverse("ticket_notifications") + "?since_id=0")
        data = response.json()

        assert data["new_tickets"][0]["category_display"] == "Unknown"

    def test_caps_at_10_results(self, ticketing_support_user, team1):
        """Should return at most 10 new tickets."""
        for i in range(15):
            Ticket.objects.create(
                team=team1,
                ticket_number=f"T{i:03}",
                category=TicketCategory.objects.get(pk=6),
                title=f"Ticket {i}",
                status="open",
            )
        client = Client()
        client.force_login(ticketing_support_user)

        response = client.get(reverse("ticket_notifications") + "?since_id=0")
        data = response.json()

        assert len(data["new_tickets"]) == 10
        assert data["open_count"] == 15

    def test_invalid_since_id_treated_as_zero(self, ticketing_support_user, team1):
        """Invalid since_id should be treated as 0."""
        Ticket.objects.create(
            team=team1, ticket_number="T001", category=TicketCategory.objects.get(pk=6), title="A", status="open"
        )
        client = Client()
        client.force_login(ticketing_support_user)

        response = client.get(reverse("ticket_notifications") + "?since_id=abc")
        data = response.json()

        assert data["open_count"] == 1
        assert len(data["new_tickets"]) == 1

    def test_forbidden_for_unprivileged_user(self, blue_team_user):
        """Blue team users should get 403."""
        client = Client()
        client.force_login(blue_team_user)

        response = client.get(reverse("ticket_notifications"))

        assert response.status_code == 403

    def test_only_returns_open_tickets_in_new_tickets(self, ticketing_support_user, team1):
        """Resolved/cancelled tickets should not appear in new_tickets."""
        Ticket.objects.create(
            team=team1, ticket_number="T001", category=TicketCategory.objects.get(pk=6), title="Open", status="open"
        )
        Ticket.objects.create(
            team=team1,
            ticket_number="T002",
            category=TicketCategory.objects.get(pk=6),
            title="Resolved",
            status="resolved",
        )
        client = Client()
        client.force_login(ticketing_support_user)

        response = client.get(reverse("ticket_notifications") + "?since_id=0")
        data = response.json()

        assert len(data["new_tickets"]) == 1
        assert data["new_tickets"][0]["number"] == "T001"
