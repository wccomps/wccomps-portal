"""Ticket lifecycle integration tests.

Exercises the full ticket state machine through Django views,
verifying cross-role interactions and state transitions.
"""

import pytest
from django.test import Client
from django.urls import reverse

from team.models import Team
from ticketing.models import Ticket, TicketCategory, TicketComment

pytestmark = pytest.mark.django_db


@pytest.fixture
def team(db, blue_team):
    """Create a team for ticket tests."""
    return blue_team


@pytest.fixture
def team_2(db):
    """Create a second team for cross-team tests."""
    return Team.objects.create(team_number=2, team_name="Test Team 2", authentik_group="WCComps_BlueTeam02")


@pytest.fixture
def category(db):
    """Get the seeded 'Other / General Issue' category."""
    return TicketCategory.objects.get(pk=6)


@pytest.fixture
def open_ticket(team, category):
    """Create an open ticket."""
    return Ticket.objects.create(
        ticket_number="T001-001",
        team=team,
        category=category,
        title="Test ticket",
        status="open",
    )


class TestHappyPath:
    """Create -> claim -> resolve."""

    def test_support_claims_and_resolves_ticket(
        self, blue_team_user, ticketing_support_user, team, category, mock_quotient_client
    ):
        # Blue team creates ticket
        client = Client()
        client.force_login(blue_team_user)
        response = client.post(
            reverse("create_ticket"),
            {"title": "Server down", "category": category.pk, "description": "Help"},
        )
        assert response.status_code == 302
        ticket = Ticket.objects.get(title="Server down")
        assert ticket.status == "open"
        assert ticket.team == team

        # Support claims it
        ops_client = Client()
        ops_client.force_login(ticketing_support_user)
        response = ops_client.post(reverse("ticket_claim", kwargs={"ticket_number": ticket.ticket_number}))
        assert response.status_code == 302
        ticket.refresh_from_db()
        assert ticket.status == "claimed"
        assert ticket.assigned_to == ticketing_support_user

        # Support resolves it (category 6 has variable_points, so points_override required)
        response = ops_client.post(
            reverse("ticket_resolve", kwargs={"ticket_number": ticket.ticket_number}),
            {"resolution_notes": "Fixed the server", "points_override": "10"},
        )
        assert response.status_code == 302
        ticket.refresh_from_db()
        assert ticket.status == "resolved"
        assert ticket.resolution_notes == "Fixed the server"


class TestCancelFlow:
    """Blue team creates and cancels their own ticket."""

    def test_team_cancels_open_ticket(self, blue_team_user, open_ticket):
        client = Client()
        client.force_login(blue_team_user)
        response = client.post(reverse("ticket_cancel", kwargs={"ticket_number": open_ticket.ticket_number}))
        assert response.status_code == 302
        open_ticket.refresh_from_db()
        assert open_ticket.status == "cancelled"

    def test_cannot_cancel_claimed_ticket(self, blue_team_user, open_ticket, ticketing_support_user):
        # Claim the ticket first
        open_ticket.status = "claimed"
        open_ticket.assigned_to = ticketing_support_user
        open_ticket.save()

        client = Client()
        client.force_login(blue_team_user)
        client.post(reverse("ticket_cancel", kwargs={"ticket_number": open_ticket.ticket_number}))
        # Should fail — ticket is not open (view renders error page, 200)
        open_ticket.refresh_from_db()
        assert open_ticket.status == "claimed"


class TestReopenFlow:
    """Create -> claim -> resolve -> reopen -> reclaim -> re-resolve."""

    def test_full_reopen_cycle(self, ticketing_admin_user, ticketing_support_user, open_ticket):
        tn = open_ticket.ticket_number
        ops_client = Client()
        ops_client.force_login(ticketing_support_user)
        admin_client = Client()
        admin_client.force_login(ticketing_admin_user)

        # Claim
        ops_client.post(reverse("ticket_claim", kwargs={"ticket_number": tn}))
        open_ticket.refresh_from_db()
        assert open_ticket.status == "claimed"

        # Resolve (category 6 has variable_points, so points_override required)
        ops_client.post(
            reverse("ticket_resolve", kwargs={"ticket_number": tn}),
            {"points_override": "10"},
        )
        open_ticket.refresh_from_db()
        assert open_ticket.status == "resolved"

        # Reopen (requires ticketing_admin)
        admin_client.post(
            reverse("ticket_reopen", kwargs={"ticket_number": tn}),
            {"reopen_reason": "Needs more work"},
        )
        open_ticket.refresh_from_db()
        assert open_ticket.status == "open"
        assert open_ticket.assigned_to is None

        # Reclaim and re-resolve
        ops_client.post(reverse("ticket_claim", kwargs={"ticket_number": tn}))
        open_ticket.refresh_from_db()
        assert open_ticket.status == "claimed"

        ops_client.post(
            reverse("ticket_resolve", kwargs={"ticket_number": tn}),
            {"points_override": "10"},
        )
        open_ticket.refresh_from_db()
        assert open_ticket.status == "resolved"


class TestInvalidTransitions:
    """Verify the state machine rejects illegal transitions."""

    def test_cannot_resolve_open_ticket(self, ticketing_support_user, open_ticket):
        """Support can't resolve a ticket they haven't claimed."""
        client = Client()
        client.force_login(ticketing_support_user)
        response = client.post(reverse("ticket_resolve", kwargs={"ticket_number": open_ticket.ticket_number}))
        # Should be denied (not assigned to this user)
        assert response.status_code == 403
        open_ticket.refresh_from_db()
        assert open_ticket.status == "open"

    def test_cannot_reopen_cancelled_ticket(self, ticketing_admin_user, open_ticket):
        """Admin can't reopen a cancelled ticket."""
        open_ticket.status = "cancelled"
        open_ticket.save()

        client = Client()
        client.force_login(ticketing_admin_user)
        client.post(
            reverse("ticket_reopen", kwargs={"ticket_number": open_ticket.ticket_number}),
            {"reopen_reason": "Try again"},
        )
        open_ticket.refresh_from_db()
        assert open_ticket.status == "cancelled"

    def test_support_cannot_reopen(self, ticketing_support_user, open_ticket):
        """Only ticketing_admin can reopen, not ticketing_support."""
        open_ticket.status = "resolved"
        open_ticket.save()

        client = Client()
        client.force_login(ticketing_support_user)
        response = client.post(reverse("ticket_reopen", kwargs={"ticket_number": open_ticket.ticket_number}))
        assert response.status_code == 403
        open_ticket.refresh_from_db()
        assert open_ticket.status == "resolved"


class TestCrossRole:
    """Different roles interact on the same ticket."""

    def test_blue_creates_support_claims_admin_resolves(
        self, blue_team_user, ticketing_support_user, ticketing_admin_user, open_ticket
    ):
        tn = open_ticket.ticket_number

        # Support claims
        support_client = Client()
        support_client.force_login(ticketing_support_user)
        support_client.post(reverse("ticket_claim", kwargs={"ticket_number": tn}))
        open_ticket.refresh_from_db()
        assert open_ticket.status == "claimed"
        assert open_ticket.assigned_to == ticketing_support_user

        # Admin can resolve anyone's ticket (category 6 requires points_override)
        admin_client = Client()
        admin_client.force_login(ticketing_admin_user)
        admin_client.post(
            reverse("ticket_resolve", kwargs={"ticket_number": tn}),
            {"resolution_notes": "Admin resolved", "points_override": "10"},
        )
        open_ticket.refresh_from_db()
        assert open_ticket.status == "resolved"

    def test_other_team_cannot_cancel(self, blue_team_02_user, team_2, open_ticket):
        """A different blue team can't cancel another team's ticket."""
        client = Client()
        client.force_login(blue_team_02_user)
        client.post(reverse("ticket_cancel", kwargs={"ticket_number": open_ticket.ticket_number}))
        open_ticket.refresh_from_db()
        assert open_ticket.status == "open"


class TestComments:
    """Verify comment creation on tickets."""

    def test_ops_can_comment(self, ticketing_support_user, open_ticket):
        client = Client()
        client.force_login(ticketing_support_user)
        response = client.post(
            reverse("ticket_comment", kwargs={"ticket_number": open_ticket.ticket_number}),
            {"comment": "Looking into this"},
        )
        assert response.status_code == 302
        assert TicketComment.objects.filter(ticket=open_ticket, comment_text="Looking into this").exists()

    def test_team_can_comment_own_ticket(self, blue_team_user, open_ticket):
        client = Client()
        client.force_login(blue_team_user)
        response = client.post(
            reverse("ticket_comment", kwargs={"ticket_number": open_ticket.ticket_number}),
            {"comment": "Any update?"},
        )
        assert response.status_code == 302
        assert TicketComment.objects.filter(ticket=open_ticket, comment_text="Any update?").exists()

    def test_other_team_cannot_comment(self, blue_team_02_user, team_2, open_ticket):
        client = Client()
        client.force_login(blue_team_02_user)
        response = client.post(
            reverse("ticket_comment", kwargs={"ticket_number": open_ticket.ticket_number}),
            {"comment": "Sneaky comment"},
        )
        assert response.status_code == 404
        assert not TicketComment.objects.filter(comment_text="Sneaky comment").exists()


class TestBulkOperations:
    """Bulk claim and resolve."""

    def test_bulk_claim(self, ticketing_support_user, team, category):
        t1 = Ticket.objects.create(ticket_number="T001-010", team=team, category=category, title="A", status="open")
        t2 = Ticket.objects.create(ticket_number="T001-011", team=team, category=category, title="B", status="open")

        client = Client()
        client.force_login(ticketing_support_user)
        response = client.post(reverse("tickets_bulk_claim"), {"ticket_numbers": "T001-010,T001-011"})
        assert response.status_code == 302

        t1.refresh_from_db()
        t2.refresh_from_db()
        assert t1.status == "claimed"
        assert t2.status == "claimed"

    def test_bulk_resolve(self, ticketing_support_user, team, category):
        """Fixed-point tickets resolve at their category's points; variable ones need a value, so are skipped."""
        fixed = TicketCategory.objects.get(pk=2)
        t1 = Ticket.objects.create(
            ticket_number="T001-020",
            team=team,
            category=fixed,
            title="A",
            status="claimed",
            assigned_to=ticketing_support_user,
        )
        t2 = Ticket.objects.create(
            ticket_number="T001-021",
            team=team,
            category=category,
            title="B",
            status="claimed",
            assigned_to=ticketing_support_user,
        )

        client = Client()
        client.force_login(ticketing_support_user)
        response = client.post(reverse("tickets_bulk_resolve"), {"ticket_numbers": "T001-020,T001-021"}, follow=True)
        assert response.status_code == 200

        t1.refresh_from_db()
        t2.refresh_from_db()
        assert t1.status == "resolved"
        assert t1.points_charged == fixed.points
        assert t2.status == "claimed"
        assert "T001-021" in response.content.decode()

    def test_bulk_resolve_skips_tickets_assigned_to_others(self, ticketing_support_user, ticketing_admin_user, team):
        ticket = Ticket.objects.create(
            ticket_number="T001-022",
            team=team,
            category=TicketCategory.objects.get(pk=2),
            title="A",
            status="claimed",
            assigned_to=ticketing_admin_user,
        )

        client = Client()
        client.force_login(ticketing_support_user)
        client.post(reverse("tickets_bulk_resolve"), {"ticket_numbers": "T001-022"})

        ticket.refresh_from_db()
        assert ticket.status == "claimed"

    def test_blue_team_cannot_bulk_claim(self, blue_team_user, team, category):
        Ticket.objects.create(ticket_number="T001-030", team=team, category=category, title="A", status="open")

        client = Client()
        client.force_login(blue_team_user)
        response = client.post(reverse("tickets_bulk_claim"), {"ticket_numbers": "T001-030"})
        assert response.status_code == 403
