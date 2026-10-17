"""A bulk Claim or Resolve with no tickets selected goes back to the list with a message, like the bulk approvals;
a bulk or single ticket action returns to the list as it was filtered (or the ticket's page), through back_to_list
alone."""

import re

import pytest
from django.contrib.auth.models import User
from django.contrib.messages import get_messages
from django.test import Client
from django.urls import reverse

from core.tests.guards import REPO_DIR, fail_with, tracked_files

pytestmark = pytest.mark.django_db


@pytest.fixture
def client(ticketing_support_user: User) -> Client:
    client = Client()
    client.force_login(ticketing_support_user)
    return client


@pytest.mark.parametrize("name", ["tickets_bulk_claim", "tickets_bulk_resolve"])
@pytest.mark.parametrize("selection", [None, "", ",", " , "])
def test_an_empty_selection_returns_to_the_list_with_a_message(
    client: Client, name: str, selection: str | None
) -> None:
    response = client.post(reverse(name), {} if selection is None else {"ticket_numbers": selection})
    assert response.status_code == 302
    assert response.url == reverse("ticket_list")
    assert [str(m) for m in get_messages(response.wsgi_request)] == ["No tickets selected"]


@pytest.mark.parametrize("name", ["tickets_bulk_claim", "tickets_bulk_resolve"])
@pytest.mark.parametrize(
    ("referer", "back"),
    [
        ("http://testserver/tickets/?status=open&search=a", "/tickets/?status=open&search=a"),
        ("http://testserver/tickets/T001-001/", "/tickets/"),
        ("https://elsewhere.example/tickets/?status=open", "/tickets/"),
    ],
)
def test_a_bulk_action_returns_to_the_filtered_list(client: Client, name: str, referer: str, back: str) -> None:
    response = client.post(reverse(name), {"ticket_numbers": "T999-001"}, headers={"referer": referer})
    assert response.status_code == 302
    assert response.url == back


@pytest.mark.parametrize(
    ("referer", "back"),
    [
        ("http://testserver/tickets/?status=open&search=a", "/tickets/?status=open&search=a"),
        ("http://testserver/tickets/T001-001/", "/tickets/T001-001/"),
        ("http://testserver/scoring/leaderboard/", "/tickets/"),
        ("http://testserver/tickets/T001-002/", "/tickets/"),
        ("https://elsewhere.example/tickets/?status=open", "/tickets/"),
    ],
)
def test_a_ticket_action_returns_to_the_list_or_the_ticket_it_came_from(
    client: Client, referer: str, back: str
) -> None:
    """Claiming went back to any page of the site the Referer named."""
    from team.models import Team
    from ticketing.models import Ticket, TicketCategory

    team = Team.objects.create(team_number=1, team_name="Team 01")
    Ticket.objects.create(ticket_number="T001-001", team=team, category=TicketCategory.objects.get(pk=6), title="t")
    response = client.post(reverse("ticket_claim", args=["T001-001"]), headers={"referer": referer})
    assert response.status_code == 302
    assert response.url == back


REFERER_RE = re.compile(r"""HTTP_REFERER|["']referer["']""", re.IGNORECASE)
# "file:line" -> why that code reads the Referer
REFERER_EXCEPTIONS = {
    "web/ticketing/views/list.py:*": "back_to_list, the one way back to the page a POST came from",
    "web/core/middleware.py:*": "the legacy-host log line names the page a request came from",
}


def test_only_back_to_list_follows_the_referer() -> None:
    """The five ticket actions followed the Referer to any page of the site; the bulk views had a copy of their own."""
    found = {
        f"{name}:{number}": line.strip()
        for name in tracked_files()
        if name.startswith("web/") and name.endswith(".py") and "/tests/" not in name
        for number, line in enumerate((REPO_DIR / name).read_text(encoding="utf-8").splitlines(), 1)
        if REFERER_RE.search(line)
    }
    fail_with("Code reading the Referer", found, REFERER_EXCEPTIONS)
