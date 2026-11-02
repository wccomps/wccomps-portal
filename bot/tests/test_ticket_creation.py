"""Tests for shared ticket creation utilities."""

import pytest
from asgiref.sync import sync_to_async

from team.models import Team
from ticketing.models import TicketCategory, TicketHistory
from ticketing.utils import create_ticket_atomic


@pytest.mark.django_db
class TestTicketCreationAtomic:
    """Test atomic ticket creation utility functions."""

    def test_create_ticket_atomic_generates_sequential_numbers(self):
        """Test that ticket numbers are generated sequentially."""
        team = Team.objects.create(
            team_name="Test Team",
            team_number=1,
            ticket_counter=0,
        )
        cat = TicketCategory.objects.get(pk=6)

        # Create first ticket
        ticket1 = create_ticket_atomic(
            team=team,
            category=cat,
            title="First Ticket",
            description="Test",
            actor_username="test_user",
        )

        assert ticket1.ticket_number == "T001-001"
        assert ticket1.team == team
        assert ticket1.status == "open"

        # Create second ticket
        ticket2 = create_ticket_atomic(
            team=team,
            category=cat,
            title="Second Ticket",
            description="Test",
            actor_username="test_user",
        )

        assert ticket2.ticket_number == "T001-002"

        # Verify counter was incremented
        team.refresh_from_db()
        assert team.ticket_counter == 2

    def test_create_ticket_atomic_creates_history(self):
        """Test that ticket history is created."""
        team = Team.objects.create(
            team_name="Test Team",
            team_number=5,
            ticket_counter=0,
        )
        cat = TicketCategory.objects.get(pk=6)

        ticket = create_ticket_atomic(
            team=team,
            category=cat,
            title="Test Ticket",
            description="Test description",
            actor_username="web_user",
        )

        # Check history was created
        history = TicketHistory.objects.filter(ticket=ticket).first()
        assert history is not None
        assert history.action == "created"
        assert history.details["created_by"] == "web_user"

    def test_create_ticket_atomic_with_optional_fields(self):
        """Test ticket creation with all optional fields."""
        team = Team.objects.create(
            team_name="Test Team",
            team_number=10,
            ticket_counter=5,
        )
        cat = TicketCategory.objects.get(pk=2)

        ticket = create_ticket_atomic(
            team=team,
            category=cat,
            title="Box Reset",
            description="Reset needed",
            hostname="web01",
            ip_address="10.0.1.50",
            service_name="HTTP",
            actor_username="admin",
        )

        assert ticket.ticket_number == "T010-006"
        assert ticket.hostname == "web01"
        assert ticket.ip_address == "10.0.1.50"
        assert ticket.service_name == "HTTP"

    def test_create_ticket_atomic_with_f_expression_and_update_fields(self):
        """Test that F() expression with update_fields works (regression test)."""
        team = Team.objects.create(
            team_name="Test Team",
            team_number=50,
            ticket_counter=0,
        )
        cat = TicketCategory.objects.get(pk=6)

        # This should not raise ValidationError
        ticket = create_ticket_atomic(
            team=team,
            category=cat,
            title="Test",
            description="Test",
            actor_username="test",
        )

        assert ticket is not None
        assert ticket.ticket_number == "T050-001"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
class TestAsyncTicketCreationAtomic:
    """Test async ticket creation utility functions."""

    async def test_create_ticket_atomic_from_async_generates_sequential_numbers(self, other_category):
        """Test async ticket number generation."""
        team = await Team.objects.acreate(
            team_name="Test Team",
            team_number=2,
            ticket_counter=0,
        )

        # Create first ticket
        ticket1 = await sync_to_async(create_ticket_atomic)(
            team=team,
            category=other_category,
            title="First Ticket",
            description="Test",
            actor_username="discord:testuser",
        )

        assert ticket1.ticket_number == "T002-001"

        # Create second ticket
        ticket2 = await sync_to_async(create_ticket_atomic)(
            team=team,
            category=other_category,
            title="Second Ticket",
            description="Test",
            actor_username="discord:testuser",
        )

        assert ticket2.ticket_number == "T002-002"

        # Verify counter
        await team.arefresh_from_db()
        assert team.ticket_counter == 2

    async def test_create_ticket_atomic_from_async_creates_history(self, other_category):
        """Test async ticket history creation."""
        team = await Team.objects.acreate(
            team_name="Test Team",
            team_number=3,
            ticket_counter=0,
        )

        ticket = await sync_to_async(create_ticket_atomic)(
            team=team,
            category=other_category,
            title="Test",
            description="Test",
            actor_username="discord:user#1234",
        )

        # Check history
        history = await TicketHistory.objects.filter(ticket=ticket).afirst()
        assert history is not None
        assert history.action == "created"
        assert history.details["created_by"] == "discord:user#1234"

    async def test_create_ticket_atomic_from_async_concurrent_safety(self, other_category):
        """Test that concurrent ticket creation doesn't create duplicate numbers."""
        import asyncio

        team = await Team.objects.acreate(
            team_name="Test Team",
            team_number=25,
            ticket_counter=0,
        )

        # Create multiple tickets concurrently
        tasks = [
            sync_to_async(create_ticket_atomic)(
                team=team,
                category=other_category,
                title=f"Ticket {i}",
                description="Test",
                actor_username=f"user{i}",
            )
            for i in range(5)
        ]

        tickets = await asyncio.gather(*tasks)

        # Get all ticket numbers
        ticket_numbers = [t.ticket_number for t in tickets]

        # All should be unique
        assert len(ticket_numbers) == len(set(ticket_numbers))

        # All should follow the pattern
        assert all(tn.startswith("T025-") for tn in ticket_numbers)

        # Verify final counter
        await team.arefresh_from_db()
        assert team.ticket_counter == 5


@pytest.mark.django_db
@pytest.mark.parametrize(("staff", "filed"), [(False, False), (True, True)], ids=["team", "staff"])
def test_file_ticket_answers_the_rate_limit_and_staff_file_past_it(staff: bool, filed: bool) -> None:
    from bot.ticket_input import file_ticket
    from ticketing.utils import TEAM_TICKET_LIMIT

    team = Team.objects.create(team_number=44, team_name="Team 44")
    category = TicketCategory.objects.create(pk=95, display_name="Other", sort_order=95, user_creatable=True)
    for n in range(TEAM_TICKET_LIMIT):
        create_ticket_atomic(team=team, category=category, title=f"t{n}")

    result = file_ticket(team, "95", staff=staff, actor="discord:someone", description="help")

    assert isinstance(result, str) is not filed
    if not filed:
        assert "too many tickets" in result
