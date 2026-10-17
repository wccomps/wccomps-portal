"""Resolving, unclaiming and recategorizing a claimed ticket is for its assignee or a ticketing admin. Another
support member is staff (no 403): the ticket's state turns them away, back to the ticket with one message, and the
list offers them neither Resolve nor Unclaim. Bulk resolve skips such tickets and says so."""

import pytest
from django.contrib.messages import get_messages
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from core.tests.roles import make_user
from team.models import Team
from ticketing.access import NOT_YOUR_TICKET
from ticketing.models import Ticket, TicketCategory

pytestmark = pytest.mark.django_db


@pytest.fixture
def claimed() -> Ticket:
    team = Team.objects.create(team_number=1, team_name="Team 01")
    holder = make_user("holder", ["WCComps_Ticketing_Support"])
    return Ticket.objects.create(
        team=team,
        category=TicketCategory.objects.get(pk=6),
        ticket_number="T001-001",
        title="Help",
        status="claimed",
        assigned_to=holder,
        assigned_at=timezone.now(),
    )


@pytest.fixture
def other_support() -> Client:
    client = Client()
    client.force_login(make_user("other", ["WCComps_Ticketing_Support"]))
    return client


@pytest.mark.parametrize(
    ("url_name", "data"),
    [
        ("ticket_resolve", {"resolution_notes": "Done"}),
        ("ticket_unclaim", {}),
        ("ticket_change_category", {"new_category": "1"}),
    ],
)
def test_another_support_member_is_sent_back_with_one_message(
    url_name: str, data: dict[str, str], claimed: Ticket, other_support: Client
) -> None:
    response = other_support.post(reverse(url_name, args=[claimed.ticket_number]), data)
    assert response.status_code == 302
    assert response.url == reverse("ticket_detail", args=[claimed.ticket_number])
    assert [str(m) for m in get_messages(response.wsgi_request)] == [NOT_YOUR_TICKET]
    claimed.refresh_from_db()
    assert (claimed.status, claimed.assigned_to.username, claimed.category_id) == ("claimed", "holder", 6)


def test_the_list_offers_resolve_and_unclaim_only_to_the_assignee(claimed: Ticket, other_support: Client) -> None:
    page = other_support.get(reverse("ticket_list")).content.decode()
    assert reverse("ticket_resolve", args=[claimed.ticket_number]) not in page
    assert reverse("ticket_unclaim", args=[claimed.ticket_number]) not in page

    holder = Client()
    holder.force_login(claimed.assigned_to)
    page = holder.get(reverse("ticket_list")).content.decode()
    assert reverse("ticket_resolve", args=[claimed.ticket_number]) in page
    assert reverse("ticket_unclaim", args=[claimed.ticket_number]) in page


def test_bulk_resolve_skips_a_ticket_held_by_someone_else_and_says_so(claimed: Ticket, other_support: Client) -> None:
    response = other_support.post(reverse("tickets_bulk_resolve"), {"ticket_numbers": [claimed.ticket_number]})
    assert response.status_code == 302
    said = [str(m) for m in get_messages(response.wsgi_request)]
    assert any(claimed.ticket_number in m for m in said), said
    claimed.refresh_from_db()
    assert claimed.status == "claimed"
