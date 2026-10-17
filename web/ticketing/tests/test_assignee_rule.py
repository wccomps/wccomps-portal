"""Resolving, unclaiming, recategorizing or giving away a ticket someone holds is for its assignee or a ticketing
admin (ticketing.lifecycle.assignee_refusal, under the row lock). Another support member is staff (no 403): the
ticket's state turns them away, back to the ticket with the lifecycle's reason, which names who holds it. The pages
offer each action only to those it lets through."""

import pytest
from django.contrib.messages import get_messages
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from core.tests.roles import make_user
from team.models import Team
from ticketing.models import Ticket, TicketCategory

pytestmark = pytest.mark.django_db


def _ticket(number: str, status: str, holder: object | None) -> Ticket:
    team, _ = Team.objects.get_or_create(team_number=1, defaults={"team_name": "Team 01"})
    return Ticket.objects.create(
        team=team,
        category=TicketCategory.objects.get(pk=6),
        ticket_number=number,
        title="Help",
        status=status,
        assigned_to=holder,
        assigned_at=timezone.now() if holder else None,
    )


@pytest.fixture
def holder():
    return make_user("holder", ["WCComps_Ticketing_Support"])


@pytest.fixture
def claimed(holder) -> Ticket:
    return _ticket("T001-001", "claimed", holder)


def _client(username: str, groups: list[str]) -> Client:
    client = Client()
    client.force_login(make_user(username, groups))
    return client


@pytest.fixture
def other_support() -> Client:
    return _client("other", ["WCComps_Ticketing_Support"])


@pytest.fixture
def ticketing_admin() -> Client:
    return _client("tadmin", ["WCComps_Ticketing_Admin"])


def _said(response) -> list[str]:
    return [str(m) for m in get_messages(response.wsgi_request)]


@pytest.mark.parametrize(
    ("url_name", "data"),
    [
        ("ticket_resolve", {"resolution_notes": "Done"}),
        ("ticket_unclaim", {}),
        ("ticket_change_category", {"new_category": "1"}),
        ("ticket_reassign", {"new_assignee_username": "other"}),
    ],
)
def test_another_support_member_is_sent_back_with_who_holds_it(
    url_name: str, data: dict[str, str], claimed: Ticket, other_support: Client
) -> None:
    response = other_support.post(reverse(url_name, args=[claimed.ticket_number]), data)
    assert response.status_code == 302
    assert response.url == reverse("ticket_detail", args=[claimed.ticket_number])
    said = _said(response)
    assert len(said) == 1 and "holder" in said[0], said
    claimed.refresh_from_db()
    assert (claimed.status, claimed.assigned_to.username, claimed.category_id) == ("claimed", "holder", 6)


def test_reassigning_a_resolved_ticket_someone_holds_is_theirs_or_an_admins(holder, other_support: Client) -> None:
    """The loophole: give a held ticket to yourself, then resolve or recategorize it."""
    resolved = _ticket("T001-002", "resolved", holder)
    response = other_support.post(
        reverse("ticket_reassign", args=[resolved.ticket_number]), {"new_assignee_username": "other"}
    )
    assert "holder" in _said(response)[0]
    resolved.refresh_from_db()
    assert resolved.assigned_to.username == "holder"


def test_an_unheld_ticket_is_claimed_first(other_support: Client) -> None:
    open_ticket = _ticket("T001-003", "open", None)
    response = other_support.post(
        reverse("ticket_resolve", args=[open_ticket.ticket_number]), {"resolution_notes": "x"}
    )
    assert _said(response) == ["Claim this ticket first."]
    open_ticket.refresh_from_db()
    assert open_ticket.status == "open"


def test_anyone_on_staff_assigns_an_unheld_ticket(other_support: Client) -> None:
    open_ticket = _ticket("T001-003", "open", None)
    make_user("helper", ["WCComps_Ticketing_Support"])
    other_support.post(
        reverse("ticket_reassign", args=[open_ticket.ticket_number]), {"new_assignee_username": "helper"}
    )
    open_ticket.refresh_from_db()
    assert (open_ticket.status, open_ticket.assigned_to.username) == ("claimed", "helper")


def test_a_ticketing_admin_works_someone_elses_ticket(claimed: Ticket, ticketing_admin: Client) -> None:
    ticketing_admin.post(reverse("ticket_change_category", args=[claimed.ticket_number]), {"new_category": "1"})
    ticketing_admin.post(reverse("ticket_resolve", args=[claimed.ticket_number]), {"resolution_notes": "Done"})
    claimed.refresh_from_db()
    assert (claimed.status, claimed.category_id) == ("resolved", 1)


def test_bulk_resolve_skips_a_ticket_someone_else_holds_and_says_who(claimed: Ticket, other_support: Client) -> None:
    response = other_support.post(reverse("tickets_bulk_resolve"), {"ticket_numbers": [claimed.ticket_number]})
    assert response.status_code == 302
    assert any(claimed.ticket_number in m and "holder" in m for m in _said(response)), _said(response)
    claimed.refresh_from_db()
    assert claimed.status == "claimed"


def test_bulk_resolve_by_a_ticketing_admin_resolves_someone_elses_ticket(
    claimed: Ticket, ticketing_admin: Client
) -> None:
    Ticket.objects.filter(pk=claimed.pk).update(category_id=1)  # a fixed-points category: bulk resolve charges it
    ticketing_admin.post(reverse("tickets_bulk_resolve"), {"ticket_numbers": [claimed.ticket_number]})
    claimed.refresh_from_db()
    assert claimed.status == "resolved"


def test_the_list_offers_resolve_and_unclaim_only_to_the_assignee(claimed: Ticket, other_support: Client) -> None:
    page = other_support.get(reverse("ticket_list")).content.decode()
    assert reverse("ticket_resolve", args=[claimed.ticket_number]) not in page
    assert reverse("ticket_unclaim", args=[claimed.ticket_number]) not in page

    holder = Client()
    holder.force_login(claimed.assigned_to)
    page = holder.get(reverse("ticket_list")).content.decode()
    assert reverse("ticket_resolve", args=[claimed.ticket_number]) in page
    assert reverse("ticket_unclaim", args=[claimed.ticket_number]) in page


def test_the_detail_page_offers_the_held_tickets_actions_only_to_who_may_take_them(
    claimed: Ticket, other_support: Client
) -> None:
    page = other_support.get(reverse("ticket_detail", args=[claimed.ticket_number])).content.decode()
    for url_name in ("ticket_resolve", "ticket_unclaim", "ticket_change_category", "ticket_reassign"):
        assert reverse(url_name, args=[claimed.ticket_number]) not in page, url_name


@pytest.mark.parametrize("status", ["open", "resolved", "cancelled"])
def test_the_detail_page_offers_no_unclaim_or_category_change_off_a_claimed_ticket(
    status: str, holder, ticketing_admin: Client
) -> None:
    ticket = _ticket("T001-004", status, holder if status != "open" else None)
    page = ticketing_admin.get(reverse("ticket_detail", args=[ticket.ticket_number])).content.decode()
    assert reverse("ticket_unclaim", args=[ticket.ticket_number]) not in page
    assert reverse("ticket_change_category", args=[ticket.ticket_number]) not in page
