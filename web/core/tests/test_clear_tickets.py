"""Tests for clear tickets functionality."""

import pytest

from core.models import AuditLog
from team.models import Team
from ticketing.models import Ticket, TicketAttachment, TicketCategory, TicketComment, TicketHistory
from ticketing.utils import clear_all_tickets

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
def setup_tickets(setup_teams: tuple[Team, Team]) -> tuple[Team, Team, list[Ticket]]:
    """Create test tickets with related data."""
    team1, team2 = setup_teams

    ticket1 = Ticket.objects.create(
        ticket_number="T001-001",
        team=team1,
        category=TicketCategory.objects.get(pk=6),
        title="Test Ticket 1",
        description="Test description",
        status="open",
    )

    ticket2 = Ticket.objects.create(
        ticket_number="T001-002",
        team=team1,
        category=TicketCategory.objects.get(pk=6),
        title="Test Ticket 2",
        description="Test description",
        status="claimed",
    )

    ticket3 = Ticket.objects.create(
        ticket_number="T002-001",
        team=team2,
        category=TicketCategory.objects.get(pk=6),
        title="Test Ticket 3",
        description="Test description",
        status="resolved",
    )

    # Add related data
    TicketComment.objects.create(
        ticket=ticket1,
        comment_text="Test comment",
    )

    TicketAttachment.objects.create(
        ticket=ticket1,
        file_data=b"test data",
        filename="test.txt",
        mime_type="text/plain",
        uploaded_by="test_user",
    )

    TicketHistory.objects.create(
        ticket=ticket1,
        action="created",
        details={"created_by": "test_user"},
    )

    return team1, team2, [ticket1, ticket2, ticket3]


@pytest.mark.django_db
class TestClearAllTickets:
    """clear_all_tickets: shared by the ops page and /tickets clear."""

    def test_clears_all_tickets(self, setup_tickets: tuple[Team, Team, list[Ticket]]) -> None:
        team1, team2, _ = setup_tickets

        counts = clear_all_tickets(actor="web:admin")

        assert counts["tickets_deleted"] == 3
        assert counts["teams_reset"] == 2
        assert Ticket.objects.count() == 0
        assert TicketComment.objects.count() == 0
        assert TicketAttachment.objects.count() == 0
        assert TicketHistory.objects.count() == 0
        team1.refresh_from_db()
        team2.refresh_from_db()
        assert (team1.ticket_counter, team2.ticket_counter) == (0, 0)
        audit = AuditLog.objects.get(action="clear_tickets")
        assert audit.admin_user == "web:admin"
        assert audit.details == counts

    def test_handles_no_tickets(self, setup_teams: tuple[Team, Team]) -> None:
        assert clear_all_tickets(actor="web:admin")["tickets_deleted"] == 0
