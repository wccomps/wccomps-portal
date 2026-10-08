"""Tests for ticket category playbook URL visibility and permissions."""

import pytest
from django.test import Client
from django.urls import reverse

from team.models import Team
from ticketing.models import Ticket, TicketCategory

pytestmark = pytest.mark.django_db


@pytest.fixture
def team(db: object) -> Team:
    return Team.objects.create(team_number=1, team_name="Team 01", authentik_group="WCComps_BlueTeam01")


@pytest.fixture
def category_with_playbook(db: object) -> TicketCategory:
    cat = TicketCategory.objects.get(pk=2)
    cat.playbook_url = "https://wiki.internal/playbooks/box-reset"
    cat.save(update_fields=["playbook_url"])
    return cat


@pytest.fixture
def category_without_playbook(db: object) -> TicketCategory:
    cat = TicketCategory.objects.get(pk=6)
    cat.playbook_url = ""
    cat.save(update_fields=["playbook_url"])
    return cat


@pytest.fixture
def ticket_with_playbook(team: Team, category_with_playbook: TicketCategory) -> Ticket:
    return Ticket.objects.create(
        ticket_number="T001-001",
        team=team,
        category=category_with_playbook,
        title="Need box reset",
        status="open",
    )


@pytest.fixture
def ticket_without_playbook(team: Team, category_without_playbook: TicketCategory) -> Ticket:
    return Ticket.objects.create(
        ticket_number="T001-002",
        team=team,
        category=category_without_playbook,
        title="General query",
        status="open",
    )


class TestTicketPlaybookVisibility:
    """Test staff and team visibility of category playbook URLs."""

    @pytest.mark.parametrize("user_fixture", ["ticketing_support_user", "ticketing_admin_user", "admin_user"])
    def test_staff_sees_playbook_link_on_ticket_page(
        self, user_fixture: str, ticket_with_playbook: Ticket, request: pytest.FixtureRequest
    ) -> None:
        """Staff with ticketing permissions see the playbook link next to category."""
        user = request.getfixturevalue(user_fixture)
        client = Client()
        client.force_login(user)

        response = client.get(reverse("ticket_detail", args=[ticket_with_playbook.ticket_number]))
        assert response.status_code == 200
        assert b"https://wiki.internal/playbooks/box-reset" in response.content
        assert b"Playbook" in response.content
        assert b'class="playbook-link"' in response.content

    def test_staff_sees_no_playbook_when_category_has_none(
        self, ticketing_support_user: object, ticket_without_playbook: Ticket
    ) -> None:
        """Categories without a playbook show nothing for staff."""
        client = Client()
        client.force_login(ticketing_support_user)

        response = client.get(reverse("ticket_detail", args=[ticket_without_playbook.ticket_number]))
        assert response.status_code == 200
        assert b'class="playbook-link"' not in response.content
        assert b">Playbook</a>" not in response.content

    def test_teams_never_see_playbook_link_on_ticket_page(
        self, blue_team_user: object, ticket_with_playbook: Ticket
    ) -> None:
        """Team viewing their own ticket never sees the playbook link or URL."""
        client = Client()
        client.force_login(blue_team_user)

        response = client.get(reverse("ticket_detail", args=[ticket_with_playbook.ticket_number]))
        assert response.status_code == 200
        assert b"https://wiki.internal/playbooks/box-reset" not in response.content
        assert b'class="playbook-link"' not in response.content
        assert b">Playbook</a>" not in response.content
        assert b"playbook-link" not in response.content

    def test_teams_never_see_playbook_link_on_ticket_list(
        self, blue_team_user: object, ticket_with_playbook: Ticket
    ) -> None:
        """Team ticket list never displays playbook URLs."""
        client = Client()
        client.force_login(blue_team_user)

        response = client.get(reverse("ticket_list"))
        assert response.status_code == 200
        assert b"https://wiki.internal/playbooks/box-reset" not in response.content
        assert b'class="playbook-link"' not in response.content
        assert b">Playbook</a>" not in response.content
        assert b"playbook-link" not in response.content

    def test_playbook_link_shown_on_categories_admin_list(
        self, admin_user: object, category_with_playbook: TicketCategory, category_without_playbook: TicketCategory
    ) -> None:
        """Ticket categories list in Ops admin displays link when present and dash when absent."""
        client = Client()
        client.force_login(admin_user)

        response = client.get(reverse("admin_categories"))
        assert response.status_code == 200
        assert b"https://wiki.internal/playbooks/box-reset" in response.content
