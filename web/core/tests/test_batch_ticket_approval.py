"""Tests for batch ticket points approval functionality."""

from typing import Any

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from core.models import UserGroups
from team.models import Team
from ticketing.models import Ticket, TicketCategory, TicketHistory

pytestmark = pytest.mark.django_db


@pytest.fixture
def setup_teams() -> tuple[Team, Team]:
    """Create test teams."""
    team1 = Team.objects.create(
        team_name="Team Alpha",
        team_number=1,
        ticket_counter=5,
    )
    team2 = Team.objects.create(
        team_name="Team Beta",
        team_number=2,
        ticket_counter=3,
    )
    return team1, team2


@pytest.fixture
def setup_resolved_tickets(setup_teams: tuple[Team, Team]) -> tuple[Team, Team, list[Ticket]]:
    """Create resolved test tickets with varying verification status."""
    team1, team2 = setup_teams

    # Create resolved tickets that need verification
    ticket1 = Ticket.objects.create(
        ticket_number="T001-001",
        team=team1,
        category=TicketCategory.objects.get(pk=2),
        title="Box Reset",
        description="Test description",
        status="resolved",
        resolved_at=timezone.now(),
        points_charged=60,
        is_approved=False,
    )

    ticket2 = Ticket.objects.create(
        ticket_number="T001-002",
        team=team1,
        category=TicketCategory.objects.get(pk=3),
        title="Service Check",
        description="Test description",
        status="resolved",
        resolved_at=timezone.now(),
        points_charged=10,
        is_approved=False,
    )

    ticket3 = Ticket.objects.create(
        ticket_number="T002-001",
        team=team2,
        category=TicketCategory.objects.get(pk=4),
        title="Phone Consultation",
        description="Test description",
        status="resolved",
        resolved_at=timezone.now(),
        points_charged=100,
        is_approved=False,
    )

    # Create already verified ticket
    ticket4 = Ticket.objects.create(
        ticket_number="T002-002",
        team=team2,
        category=TicketCategory.objects.get(pk=6),
        title="Other Issue",
        description="Test description",
        status="resolved",
        resolved_at=timezone.now(),
        points_charged=0,
        is_approved=True,
    )

    # Create open ticket (should not be affected)
    ticket5 = Ticket.objects.create(
        ticket_number="T001-003",
        team=team1,
        category=TicketCategory.objects.get(pk=6),
        title="Open Ticket",
        description="Test description",
        status="open",
        points_charged=0,
        is_approved=False,
    )

    return team1, team2, [ticket1, ticket2, ticket3, ticket4, ticket5]


@pytest.fixture
def setup_users_and_auth(setup_teams: tuple[Team, Team]) -> dict[str, Any]:
    """Create test users with authentication."""
    team1, team2 = setup_teams

    # Ticketing Admin user
    admin_user = User.objects.create_user(username="admin_user", password="test123")
    UserGroups.objects.create(user=admin_user, authentik_id="admin_uid", groups=["WCComps_Ticketing_Admin"])

    # Ticketing Support user (not admin)
    support_user = User.objects.create_user(username="support_user", password="test123")
    UserGroups.objects.create(user=support_user, authentik_id="support_uid", groups=["WCComps_Ticketing_Support"])

    # Team user
    team_user = User.objects.create_user(username="team_user", password="test123")
    UserGroups.objects.create(user=team_user, authentik_id="team_uid", groups=["WCComps_BlueTeam01"])

    # Admin user (general admin, not ticketing admin)
    general_admin = User.objects.create_user(username="general_admin", password="test123")
    UserGroups.objects.create(user=general_admin, authentik_id="general_admin_uid", groups=["WCComps_Admin"])

    return {
        "admin_user": admin_user,
        "support_user": support_user,
        "team_user": team_user,
        "general_admin": general_admin,
    }


@pytest.mark.django_db
class TestBatchTicketApproval:
    """Test batch ticket points approval functionality."""

    def test_batch_approve_requires_ticketing_admin_permission(
        self, setup_resolved_tickets: tuple[Team, Team, list[Ticket]], setup_users_and_auth: dict[str, Any]
    ) -> None:
        """Test only ticketing admins or gold team can batch approve ticket points."""
        client = Client()

        # Support user should be denied (the 403)
        client.force_login(setup_users_and_auth["support_user"])
        response = client.post("/ops/tickets/batch-verify-points/")
        assert response.status_code == 403

        # Team user should be denied
        client.force_login(setup_users_and_auth["team_user"])
        response = client.post("/ops/tickets/batch-verify-points/")
        assert response.status_code == 403

        # General admin (non-ticketing, non-gold) should be denied
        client.force_login(setup_users_and_auth["general_admin"])
        response = client.post("/ops/tickets/batch-verify-points/")
        assert response.status_code == 403

    def test_batch_approve_requires_post_method(
        self, setup_resolved_tickets: tuple[Team, Team, list[Ticket]], setup_users_and_auth: dict[str, Any]
    ) -> None:
        """Test batch approve endpoint requires POST."""
        client = Client()
        client.force_login(setup_users_and_auth["admin_user"])

        response = client.get("/ops/tickets/batch-verify-points/")
        assert response.status_code == 405

    def test_batch_approve_selected_tickets(
        self, setup_resolved_tickets: tuple[Team, Team, list[Ticket]], setup_users_and_auth: dict[str, Any]
    ) -> None:
        """Test admin can batch approve selected unverified resolved tickets."""
        team1, team2, tickets = setup_resolved_tickets
        client = Client()
        client.force_login(setup_users_and_auth["admin_user"])

        # Verify initial state - 3 unverified resolved tickets
        unverified_resolved = Ticket.objects.filter(status="resolved", is_approved=False)
        assert unverified_resolved.count() == 3

        # Already verified ticket
        verified_tickets = Ticket.objects.filter(is_approved=True)
        assert verified_tickets.count() == 1

        # Open ticket should exist
        open_tickets = Ticket.objects.filter(status="open")
        assert open_tickets.count() == 1

        # Submit with all 3 unverified ticket IDs
        ticket_ids = [str(t.id) for t in tickets[:3]]  # tickets 1, 2, 3
        response = client.post("/ops/tickets/batch-verify-points/", {"ticket_ids": ticket_ids})

        # Should redirect to review page
        assert response.status_code == 302
        assert response["Location"] == "/ops/review-tickets/"

        # Verify all resolved tickets are now verified
        unverified_resolved = Ticket.objects.filter(status="resolved", is_approved=False)
        assert unverified_resolved.count() == 0

        verified_resolved = Ticket.objects.filter(status="resolved", is_approved=True)
        assert verified_resolved.count() == 4  # 3 newly verified + 1 already verified

        # Open ticket should remain unverified
        open_ticket = Ticket.objects.get(status="open")
        assert open_ticket.is_approved is False

        # Check that all newly verified tickets have correct metadata
        for ticket_number in ["T001-001", "T001-002", "T002-001"]:
            ticket = Ticket.objects.get(ticket_number=ticket_number)
            assert ticket.is_approved is True
            assert ticket.approved_by == setup_users_and_auth["admin_user"]
            assert ticket.approved_at is not None

    def test_batch_approve_creates_history_entries(
        self, setup_resolved_tickets: tuple[Team, Team, list[Ticket]], setup_users_and_auth: dict[str, Any]
    ) -> None:
        """Test batch approval creates history entries for each verified ticket."""
        team1, team2, tickets = setup_resolved_tickets
        client = Client()
        client.force_login(setup_users_and_auth["admin_user"])

        initial_history_count = TicketHistory.objects.filter(action="points_verified").count()
        assert initial_history_count == 0

        # Submit with all 3 unverified ticket IDs
        ticket_ids = [str(t.id) for t in tickets[:3]]
        response = client.post("/ops/tickets/batch-verify-points/", {"ticket_ids": ticket_ids})
        assert response.status_code == 302

        # Check history entries created
        history_entries = TicketHistory.objects.filter(action="points_verified")
        assert history_entries.count() == 3  # 3 tickets were verified

        # Verify history details
        for entry in history_entries:
            assert entry.details.get("verified_by") == "admin_user"
            assert "batch" in entry.details
            assert entry.details["batch"] is True
            assert "points_charged" in entry.details

    def test_batch_approve_with_no_tickets_selected(
        self, setup_teams: tuple[Team, Team], setup_users_and_auth: dict[str, Any]
    ) -> None:
        """Test batch approve handles case with no tickets selected."""
        client = Client()
        client.force_login(setup_users_and_auth["admin_user"])

        response = client.post("/ops/tickets/batch-verify-points/")
        assert response.status_code == 302
        assert response["Location"] == "/ops/review-tickets/"

    def test_batch_approve_only_affects_resolved_tickets(
        self, setup_resolved_tickets: tuple[Team, Team, list[Ticket]], setup_users_and_auth: dict[str, Any]
    ) -> None:
        """Test batch approve only affects resolved tickets, not open/claimed/cancelled."""
        team1, team2, tickets = setup_resolved_tickets
        client = Client()

        # Create additional tickets in different states
        claimed_ticket = Ticket.objects.create(
            ticket_number="T001-004",
            team=team1,
            category=TicketCategory.objects.get(pk=6),
            title="Claimed Ticket",
            status="claimed",
            is_approved=False,
        )

        cancelled_ticket = Ticket.objects.create(
            ticket_number="T001-005",
            team=team1,
            category=TicketCategory.objects.get(pk=6),
            title="Cancelled Ticket",
            status="cancelled",
            is_approved=False,
        )

        client.force_login(setup_users_and_auth["admin_user"])

        # Submit all ticket IDs including non-resolved ones
        all_ids = [str(t.id) for t in tickets] + [str(claimed_ticket.id), str(cancelled_ticket.id)]
        response = client.post("/ops/tickets/batch-verify-points/", {"ticket_ids": all_ids})
        assert response.status_code == 302

        # Verify only resolved tickets were affected
        claimed_ticket.refresh_from_db()
        assert claimed_ticket.is_approved is False

        cancelled_ticket.refresh_from_db()
        assert cancelled_ticket.is_approved is False

        open_ticket = Ticket.objects.get(ticket_number="T001-003")
        assert open_ticket.is_approved is False

        # Resolved tickets should be verified (3 newly + 1 already, but ticket4 was already approved so not re-approved)
        resolved_verified = Ticket.objects.filter(status="resolved", is_approved=True).count()
        assert resolved_verified == 4  # 3 newly verified + 1 already verified

    def test_batch_approve_preserves_existing_verified_tickets(
        self, setup_resolved_tickets: tuple[Team, Team, list[Ticket]], setup_users_and_auth: dict[str, Any]
    ) -> None:
        """Test batch approve preserves metadata of already verified tickets."""
        team1, team2, tickets = setup_resolved_tickets
        client = Client()

        # Get the already verified ticket
        already_verified = Ticket.objects.get(ticket_number="T002-002")
        assert already_verified.is_approved is True
        original_verified_at = already_verified.approved_at
        original_verified_by = already_verified.approved_by

        client.force_login(setup_users_and_auth["admin_user"])

        # Submit the already-verified ticket ID
        response = client.post("/ops/tickets/batch-verify-points/", {"ticket_ids": [str(already_verified.id)]})
        assert response.status_code == 302

        # Check that already verified ticket was not modified
        already_verified.refresh_from_db()
        assert already_verified.is_approved is True
        assert already_verified.approved_at == original_verified_at
        assert already_verified.approved_by == original_verified_by

    def test_batch_approve_selected_subset(
        self, setup_resolved_tickets: tuple[Team, Team, list[Ticket]], setup_users_and_auth: dict[str, Any]
    ) -> None:
        """Test batch approve only approves selected tickets, not all pending."""
        team1, team2, tickets = setup_resolved_tickets
        client = Client()
        client.force_login(setup_users_and_auth["admin_user"])

        # Only select ticket 1 (T001-001)
        response = client.post("/ops/tickets/batch-verify-points/", {"ticket_ids": [str(tickets[0].id)]})
        assert response.status_code == 302

        # Only ticket 1 should be approved
        tickets[0].refresh_from_db()
        assert tickets[0].is_approved is True

        # Tickets 2 and 3 should still be pending
        tickets[1].refresh_from_db()
        assert tickets[1].is_approved is False
        tickets[2].refresh_from_db()
        assert tickets[2].is_approved is False

        # Only 1 history entry created
        assert TicketHistory.objects.filter(action="points_verified").count() == 1

    def test_approve_selected_button_visible_on_review_page(
        self, setup_resolved_tickets: tuple[Team, Team, list[Ticket]], setup_users_and_auth: dict[str, Any]
    ) -> None:
        """Test Approve Selected button is visible on review tickets page."""
        client = Client()
        client.force_login(setup_users_and_auth["admin_user"])

        response = client.get("/ops/review-tickets/")
        assert response.status_code == 200
        assert b"Approve Selected" in response.content
        assert b"/ops/tickets/batch-verify-points/" in response.content
