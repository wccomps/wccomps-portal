"""The ticket list's refresh keeps the query the list was opened with, and the ticket page's way back returns to
the list as it was last shown, after any action on the ticket."""

from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

import pytest
from django.test import Client
from django.urls import reverse

from team.models import Team
from ticketing.models import Ticket, TicketCategory

pytestmark = pytest.mark.django_db

QUERY = {"status": ["open"], "search": ["a&b"], "sort": ["-created_at"], "page": ["2"], "page_size": ["25"]}


class Attributes(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.found: list[dict[str, str | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.found.append(dict(attrs))


def query_of(url: str | None) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url or "").query, keep_blank_values=True)


def attributes(html: str) -> list[dict[str, str | None]]:
    parser = Attributes()
    parser.feed(html)
    return parser.found


@pytest.fixture
def client(ticketing_support_user) -> Client:
    client = Client()
    client.force_login(ticketing_support_user)
    return client


@pytest.fixture
def ticket() -> Ticket:
    team = Team.objects.create(team_number=1, team_name="Team 01")
    return Ticket.objects.create(
        ticket_number="T001-001", team=team, category=TicketCategory.objects.get(pk=6), title="a&b", status="open"
    )


def test_the_refresh_keeps_the_page_and_filters(client: Client, ticket: Ticket) -> None:
    html = client.get(reverse("ticket_list"), QUERY).content.decode()
    refresh = next(a for a in attributes(html) if a.get("id") == "ticket-list-content")
    assert query_of(refresh["hx-get"]) == QUERY


def list_link_queries(client: Client, ticket: Ticket) -> list[dict[str, list[str]]]:
    """The queries of the ticket page's links to the list: the menu's, and the breadcrumb's way back."""
    html = client.get(reverse("ticket_detail", kwargs={"ticket_number": ticket.ticket_number})).content.decode()
    return [
        query_of(a["href"]) for a in attributes(html) if urlsplit(a.get("href") or "").path == reverse("ticket_list")
    ]


def test_after_filtering_opening_and_claiming_the_back_link_returns_to_the_same_list(
    client: Client, ticket: Ticket
) -> None:
    query = {**QUERY, "page": ["1"]}
    client.get(reverse("ticket_list"), query)
    assert query in list_link_queries(client, ticket)
    response = client.post(reverse("ticket_claim", kwargs={"ticket_number": ticket.ticket_number}))
    assert response.status_code == 302
    ticket.refresh_from_db()
    assert ticket.status == "claimed"
    assert query in list_link_queries(client, ticket)


def test_a_ticket_link_carries_no_list_query(client: Client, ticket: Ticket) -> None:
    html = client.get(reverse("ticket_list"), QUERY).content.decode()
    detail_url = reverse("ticket_detail", kwargs={"ticket_number": ticket.ticket_number})
    links = [a["href"] for a in attributes(html) if (a.get("href") or "").startswith(detail_url)]
    assert links == [detail_url]
