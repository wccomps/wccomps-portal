"""The ticket list's refresh keeps the query the list was opened with (the ticket page's way back is per tab, in
the browser: browser_tests/test_ticket_back_link.py)."""

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


def test_a_ticket_link_carries_no_list_query(client: Client, ticket: Ticket) -> None:
    html = client.get(reverse("ticket_list"), QUERY).content.decode()
    detail_url = reverse("ticket_detail", kwargs={"ticket_number": ticket.ticket_number})
    assert [a["href"] for a in attributes(html) if (a.get("href") or "").startswith(detail_url)] == [detail_url]
