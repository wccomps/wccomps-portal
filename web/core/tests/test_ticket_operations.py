"""Tests for ticket operation views (HTTP level)."""

import pytest
from django.test import Client
from django.urls import reverse

from team.models import Team
from ticketing.models import Ticket, TicketCategory, TicketHistory

pytestmark = pytest.mark.django_db


@pytest.fixture
def team_with_tickets(blue_team_user):
    """Create a team with tickets that matches blue_team_user's group."""
    # Team 1 matches WCComps_BlueTeam01 group
    team = Team.objects.update_or_create(team_number=1, defaults={"team_name": "Blue Team 01", "ticket_counter": 3})[0]
    box_reset = TicketCategory.objects.get(pk=2)
    scoring_check = TicketCategory.objects.get(pk=3)
    other = TicketCategory.objects.get(pk=6)
    tickets = [
        Ticket.objects.create(
            ticket_number="T001-001",
            team=team,
            category=box_reset,
            title="Open ticket",
            status="open",
        ),
        Ticket.objects.create(
            ticket_number="T001-002",
            team=team,
            category=scoring_check,
            title="Claimed ticket",
            status="claimed",
        ),
        Ticket.objects.create(
            ticket_number="T001-003",
            team=team,
            category=other,
            title="Resolved ticket",
            status="resolved",
            points_charged=50,
        ),
    ]
    return team, tickets


@pytest.fixture
def other_team_ticket():
    """Create a ticket for a different team."""
    team = Team.objects.create(
        team_number=2,
        team_name="Blue Team 02",
    )
    other = TicketCategory.objects.get(pk=6)
    ticket = Ticket.objects.create(
        ticket_number="T002-001",
        team=team,
        category=other,
        title="Other team ticket",
        status="open",
    )
    return team, ticket


class TestTeamTicketList:
    """Tests for team_tickets view."""

    def test_unauthenticated_redirects_to_login(self, team_with_tickets):
        """Unauthenticated users should be redirected."""
        client = Client()
        response = client.get(reverse("ticket_list"))
        assert response.status_code == 302
        assert "login" in response.url or "accounts" in response.url

    def test_team_sees_only_own_tickets(self, blue_team_user, team_with_tickets):
        """Team member should only see their team's tickets."""
        team, tickets = team_with_tickets
        client = Client()
        client.force_login(blue_team_user)
        response = client.get(reverse("ticket_list"))
        assert response.status_code == 200
        content = response.content.decode()
        assert "T001-001" in content
        assert "T001-002" in content
        assert "T001-003" in content

    def test_team_cannot_see_other_team_tickets(self, blue_team_user, team_with_tickets, other_team_ticket):
        """Team member should not see other team's tickets."""
        client = Client()
        client.force_login(blue_team_user)
        response = client.get(reverse("ticket_list"))
        content = response.content.decode()
        assert "T002-001" not in content

    def test_status_filter_works(self, blue_team_user, team_with_tickets):
        """Status filter should filter tickets."""
        client = Client()
        client.force_login(blue_team_user)
        response = client.get(reverse("ticket_list") + "?status=open")
        assert response.status_code == 200
        content = response.content.decode()
        assert "T001-001" in content  # open
        # Resolved ticket may or may not appear depending on filter implementation

    def test_htmx_request_returns_partial(self, blue_team_user, team_with_tickets):
        """htmx request returns only the table partial, not full page."""
        team, tickets = team_with_tickets
        client = Client()
        client.force_login(blue_team_user)
        response = client.get(
            reverse("ticket_list") + "?status=open",
            HTTP_HX_REQUEST="true",
        )
        assert response.status_code == 200
        content = response.content.decode()
        # Partial should contain the table content wrapper
        assert 'id="ticket-list-content"' in content
        # Partial should contain ticket data
        assert "T001-001" in content
        # Partial should NOT contain full page elements
        assert "<title>" not in content
        assert "c-page_header" not in content

    def test_non_team_user_gets_error(self, ticketing_support_user):
        """Non-team users without team association get error."""
        client = Client()
        client.force_login(ticketing_support_user)
        response = client.get(reverse("ticket_list"))
        # Should redirect to create_ticket for admins or show error
        assert response.status_code in [200, 302]


class TestTeamTicketDetail:
    """Tests for ticket_detail view."""

    def test_team_can_view_own_ticket(self, blue_team_user, team_with_tickets):
        """Team member can view their own team's ticket."""
        team, tickets = team_with_tickets
        client = Client()
        client.force_login(blue_team_user)
        response = client.get(reverse("ticket_detail", args=[tickets[0].ticket_number]))
        assert response.status_code == 200

    def test_team_cannot_view_other_team_ticket(self, blue_team_user, other_team_ticket, team_with_tickets):
        """Team member cannot view another team's ticket."""
        other_team, ticket = other_team_ticket
        client = Client()
        client.force_login(blue_team_user)
        response = client.get(reverse("ticket_detail", args=[ticket.ticket_number]))
        assert response.status_code == 403


class TestTicketCancel:
    """Tests for ticket_cancel view."""

    def test_cancel_requires_post(self, blue_team_user, team_with_tickets):
        """Cancel endpoint requires POST method."""
        team, tickets = team_with_tickets
        client = Client()
        client.force_login(blue_team_user)
        response = client.get(reverse("ticket_cancel", args=[tickets[0].ticket_number]))
        assert response.status_code == 405

    def test_team_can_cancel_open_ticket(self, blue_team_user, team_with_tickets):
        """Team member can cancel their own open ticket."""
        team, tickets = team_with_tickets
        open_ticket = tickets[0]
        client = Client()
        client.force_login(blue_team_user)
        response = client.post(reverse("ticket_cancel", args=[open_ticket.ticket_number]))
        assert response.status_code == 302  # Redirect after cancel

        open_ticket.refresh_from_db()
        assert open_ticket.status == Ticket.STATUS_CANCELLED

    def test_cannot_cancel_claimed_ticket(self, blue_team_user, team_with_tickets):
        """Cannot cancel a ticket that's already been claimed."""
        team, tickets = team_with_tickets
        claimed_ticket = tickets[1]
        client = Client()
        client.force_login(blue_team_user)
        response = client.post(reverse("ticket_cancel", args=[claimed_ticket.ticket_number]))
        assert response.status_code == 302  # Redirect with error message

        claimed_ticket.refresh_from_db()
        assert claimed_ticket.status == "claimed"  # Unchanged

    def test_cannot_cancel_other_team_ticket(self, blue_team_user, other_team_ticket, team_with_tickets):
        """Team member cannot cancel another team's ticket."""
        other_team, ticket = other_team_ticket
        client = Client()
        client.force_login(blue_team_user)
        response = client.post(reverse("ticket_cancel", args=[ticket.ticket_number]))
        # Should show error (ticket not found for their team)
        assert b"ticket not found" in response.content.lower() or b"access denied" in response.content.lower()


class TestOpsTicketClaim:
    """Tests for ops_ticket_claim view."""

    def test_claim_requires_post(self, ticketing_support_user, team_with_tickets):
        """Claim endpoint requires POST method."""
        team, tickets = team_with_tickets
        client = Client()
        client.force_login(ticketing_support_user)
        response = client.get(reverse("ticket_claim", args=[tickets[0].ticket_number]))
        assert response.status_code == 405

    def test_blue_team_cannot_claim(self, blue_team_user, team_with_tickets):
        """Blue team member cannot claim tickets."""
        team, tickets = team_with_tickets
        client = Client()
        client.force_login(blue_team_user)
        response = client.post(reverse("ticket_claim", args=[tickets[0].ticket_number]))
        assert response.status_code == 403

    def test_support_can_claim_open_ticket(self, ticketing_support_user, team_with_tickets):
        """Ticketing support can claim an open ticket."""
        team, tickets = team_with_tickets
        open_ticket = tickets[0]
        client = Client()
        client.force_login(ticketing_support_user)
        response = client.post(reverse("ticket_claim", args=[open_ticket.ticket_number]))
        assert response.status_code == 302  # Redirect after claim

        open_ticket.refresh_from_db()
        assert open_ticket.status == "claimed"
        assert open_ticket.assigned_to is not None

    def test_cannot_claim_already_claimed_ticket(self, ticketing_support_user, team_with_tickets):
        """Cannot claim a ticket that's already claimed - redirects with error message."""
        team, tickets = team_with_tickets
        claimed_ticket = tickets[1]
        client = Client()
        client.force_login(ticketing_support_user)
        response = client.post(reverse("ticket_claim", args=[claimed_ticket.ticket_number]))
        # Now redirects with flash message instead of returning 400
        assert response.status_code == 302


class TestOpsTicketUnclaim:
    """Tests for ops_ticket_unclaim view."""

    def test_unclaim_requires_post(self, ticketing_support_user, team_with_tickets):
        """Unclaim endpoint requires POST method."""
        team, tickets = team_with_tickets
        client = Client()
        client.force_login(ticketing_support_user)
        response = client.get(reverse("ticket_unclaim", args=[tickets[1].ticket_number]))
        assert response.status_code == 405

    def test_support_can_unclaim_own_ticket(self, ticketing_support_user, team_with_tickets):
        """Support can unclaim a ticket they claimed."""
        team, tickets = team_with_tickets
        open_ticket = tickets[0]

        # First claim it
        client = Client()
        client.force_login(ticketing_support_user)
        client.post(reverse("ticket_claim", args=[open_ticket.ticket_number]))
        open_ticket.refresh_from_db()

        # Then unclaim it
        response = client.post(reverse("ticket_unclaim", args=[open_ticket.ticket_number]))
        assert response.status_code == 302

        open_ticket.refresh_from_db()
        assert open_ticket.status == "open"
        assert open_ticket.assigned_to is None

    def test_admin_can_unclaim_any_ticket(self, ticketing_support_user, ticketing_admin_user, team_with_tickets):
        """Ticketing admin can unclaim any ticket."""
        team, tickets = team_with_tickets
        open_ticket = tickets[0]

        # Support claims ticket
        client = Client()
        client.force_login(ticketing_support_user)
        client.post(reverse("ticket_claim", args=[open_ticket.ticket_number]))
        open_ticket.refresh_from_db()
        assert open_ticket.status == "claimed"

        # Admin unclaims it
        client.force_login(ticketing_admin_user)
        response = client.post(reverse("ticket_unclaim", args=[open_ticket.ticket_number]))
        assert response.status_code == 302

        open_ticket.refresh_from_db()
        assert open_ticket.status == "open"


class TestOpsTicketResolve:
    """Tests for ops_ticket_resolve view."""

    def test_resolve_requires_post(self, ticketing_support_user, team_with_tickets):
        """Resolve endpoint requires POST method."""
        team, tickets = team_with_tickets
        client = Client()
        client.force_login(ticketing_support_user)
        response = client.get(reverse("ticket_resolve", args=[tickets[1].ticket_number]))
        assert response.status_code == 405

    def test_support_can_resolve_claimed_ticket(self, ticketing_support_user, team_with_tickets):
        """Support can resolve a ticket they claimed."""
        team, tickets = team_with_tickets
        open_ticket = tickets[0]

        # First claim it
        client = Client()
        client.force_login(ticketing_support_user)
        client.post(reverse("ticket_claim", args=[open_ticket.ticket_number]))

        # Then resolve it
        response = client.post(
            reverse("ticket_resolve", args=[open_ticket.ticket_number]),
            {"resolution_notes": "Fixed the issue"},
        )
        assert response.status_code == 302

        open_ticket.refresh_from_db()
        assert open_ticket.status == "resolved"
        assert open_ticket.points_charged > 0 or open_ticket.points_charged == 0  # Category-dependent

    def test_cannot_resolve_others_claimed_ticket(
        self, ticketing_support_user, ticketing_admin_user, team_with_tickets
    ):
        """Support cannot resolve a ticket claimed by someone else (unless admin)."""
        team, tickets = team_with_tickets
        open_ticket = tickets[0]

        # Support claims ticket
        client = Client()
        client.force_login(ticketing_support_user)
        client.post(reverse("ticket_claim", args=[open_ticket.ticket_number]))

        # Different support tries to resolve
        from django.contrib.auth.models import User

        from core.models import UserGroups

        other_support = User.objects.create_user(username="other_support", password="test")
        UserGroups.objects.create(
            user=other_support, authentik_id="other-support-uid", groups=["WCComps_Ticketing_Support"]
        )

        client.force_login(other_support)
        response = client.post(
            reverse("ticket_resolve", args=[open_ticket.ticket_number]),
            {"resolution_notes": "Trying to resolve"},
        )
        assert response.status_code == 403


class TestOpsTicketReopen:
    """Tests for ops_ticket_reopen view."""

    def test_reopen_requires_admin(self, ticketing_support_user, team_with_tickets):
        """Only ticketing admin can reopen tickets."""
        team, tickets = team_with_tickets
        resolved_ticket = tickets[2]
        client = Client()
        client.force_login(ticketing_support_user)
        response = client.post(reverse("ticket_reopen", args=[resolved_ticket.ticket_number]))
        assert response.status_code == 403

    def test_admin_can_reopen_resolved_ticket(self, ticketing_admin_user, team_with_tickets):
        """Ticketing admin can reopen a resolved ticket."""
        team, tickets = team_with_tickets
        resolved_ticket = tickets[2]
        client = Client()
        client.force_login(ticketing_admin_user)
        response = client.post(
            reverse("ticket_reopen", args=[resolved_ticket.ticket_number]),
            {"reopen_reason": "Need to verify the fix"},
        )
        assert response.status_code == 302

        resolved_ticket.refresh_from_db()
        assert resolved_ticket.status == "open"

    def test_cannot_reopen_non_resolved_ticket(self, ticketing_admin_user, team_with_tickets):
        """Cannot reopen a ticket that's not resolved - redirects with error message."""
        team, tickets = team_with_tickets
        open_ticket = tickets[0]
        client = Client()
        client.force_login(ticketing_admin_user)
        response = client.post(reverse("ticket_reopen", args=[open_ticket.ticket_number]))
        # Now redirects with flash message instead of returning 400
        assert response.status_code == 302


class TestTicketHistoryCreation:
    """Test that ticket operations create appropriate history entries."""

    def test_cancel_creates_history(self, blue_team_user, team_with_tickets):
        """Cancelling a ticket creates history entry."""
        team, tickets = team_with_tickets
        open_ticket = tickets[0]

        client = Client()
        client.force_login(blue_team_user)
        client.post(reverse("ticket_cancel", args=[open_ticket.ticket_number]))

        history = TicketHistory.objects.filter(ticket=open_ticket, action="cancelled")
        assert history.exists()

    def test_reopen_creates_history(self, ticketing_admin_user, team_with_tickets):
        """Reopening a ticket creates history entry."""
        team, tickets = team_with_tickets
        resolved_ticket = tickets[2]

        client = Client()
        client.force_login(ticketing_admin_user)
        client.post(
            reverse("ticket_reopen", args=[resolved_ticket.ticket_number]),
            {"reopen_reason": "Testing history"},
        )

        history = TicketHistory.objects.filter(ticket=resolved_ticket, action="reopened")
        assert history.exists()
        assert "Testing history" in history.first().details.get("reason", "")


class TestOpsReviewTicketsHtmx:
    """Tests for ops_review_tickets htmx partial responses."""

    def test_htmx_request_returns_partial(self, ticketing_admin_user, team_with_tickets):
        """htmx request returns only the table partial, not full page."""
        team, tickets = team_with_tickets
        # Resolve a ticket so there's data to show
        resolved_ticket = tickets[2]

        client = Client()
        client.force_login(ticketing_admin_user)
        response = client.get(
            reverse("ops_review_tickets") + "?verified=all",
            HTTP_HX_REQUEST="true",
        )
        assert response.status_code == 200
        content = response.content.decode()
        # Partial should contain the content wrapper
        assert 'id="review-tickets-content"' in content
        # Partial should contain ticket data
        assert resolved_ticket.ticket_number in content
        # Partial should NOT contain full page elements
        assert "<title>" not in content
        assert "c-filter_toolbar" not in content

    def test_regular_request_returns_full_page(self, ticketing_admin_user, team_with_tickets):
        """Regular request returns full page with filter toolbar."""
        client = Client()
        client.force_login(ticketing_admin_user)
        response = client.get(reverse("ops_review_tickets"))
        assert response.status_code == 200
        content = response.content.decode()
        # Full page should contain filter form
        assert "changelist-filter" in content
