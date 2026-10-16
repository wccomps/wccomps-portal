"""A list page's filters are escaped where the page echoes them, stay one parameter in the links that carry them
on, a filter that isn't a number is ignored rather than failing the page, and every sort header sorts."""

from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from hypothesis import example, given
from hypothesis import strategies as st

from core.tests.strategies import request_text, rolled_back

pytestmark = pytest.mark.django_db

ECHOED_FILTERS = [
    ("ticket_list", "team"),
    ("ticket_list", "search"),
    ("ops_review_tickets", "search"),
    ("scoring:red_team_scores", "attack_type"),
    ("scoring:red_team_findings", "submitter"),
    ("scoring:inject_grades_review", "inject"),
    ("scoring:review_incidents", "box"),
    ("scoring:review_orange", "search"),
]


@pytest.fixture
def admin_client(admin_user: User) -> Client:
    client = Client()
    client.force_login(admin_user)
    return client


@pytest.mark.parametrize(("name", "param"), ECHOED_FILTERS)
@given(markup=st.sampled_from(["<script>alert(1)</script>", '"><img src=x onerror=alert(1)>', "<p data-probe=1>"]))
@example(markup="<script>alert(1)</script>")
@example(markup='"><svg onload=alert(1)>"')
def test_a_list_filter_is_escaped_where_the_page_echoes_it(
    admin_client: Client, name: str, param: str, markup: str
) -> None:
    with rolled_back():
        response = admin_client.get(reverse(name), {param: markup})
    assert response.status_code == 200
    assert markup not in response.content.decode()


class Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.urls: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.urls += [value for name, value in attrs if name in ("href", "hx-get") and value and "?" in value]


@pytest.mark.parametrize(("name", "param"), ECHOED_FILTERS)
def test_a_list_filter_stays_one_parameter_in_the_links_that_carry_it(
    admin_client: Client, name: str, param: str
) -> None:
    value = "a&injected=1 #b"
    response = admin_client.get(reverse(name), {param: value})
    links = Links()
    links.feed(response.content.decode())
    carried = [parse_qs(urlsplit(url).query, keep_blank_values=True) for url in links.urls]
    assert any(query.get(param) == [value] for query in carried), links.urls
    assert not [query for query in carried if "injected" in query], links.urls


NUMBER_FILTERS = [
    ("ticket_list", "team"),
    ("ticket_list", "category"),
    ("ticket_list", "assignee"),
    ("ticket_list", "page_size"),
    ("ticket_list", "page"),
    ("ops_review_tickets", "category"),
    ("ops_review_tickets", "team"),
    ("scoring:inject_grades_review", "page"),
    ("scoring:red_team_findings", "team"),
    ("scoring:red_team_findings", "attack_type"),
    ("scoring:red_team_findings", "submitter"),
    ("scoring:red_team_scores", "team"),
    ("scoring:red_team_scores", "attack_type"),
    ("scoring:red_team_scores", "submitter"),
    ("scoring:review_incidents", "team"),
    ("scoring:inject_grades_review", "team"),
    ("scoring:review_orange", "team"),
    ("scoring:review_orange", "check"),
]


@pytest.mark.parametrize(("name", "param"), NUMBER_FILTERS)
@given(value=request_text(40).filter(lambda v: "\x00" not in v))
@example(value="pending")
@example(value=":")
def test_a_list_filter_that_is_not_a_number_is_ignored(admin_client: Client, name: str, param: str, value: str) -> None:
    with rolled_back():
        response = admin_client.get(reverse(name), {param: value})
    assert response.status_code == 200


LIST_PAGES = sorted({name for name, _ in ECHOED_FILTERS})


@pytest.mark.parametrize("name", LIST_PAGES)
def test_every_sort_header_a_list_shows_is_one_its_view_sorts_by(admin_client: Client, name: str) -> None:
    """A header sorted by field links to -field next; a view that refuses the field falls back to its default."""
    links = Links()
    links.feed(admin_client.get(reverse(name)).content.decode())
    fields = {
        sort
        for url in links.urls
        for sort in parse_qs(urlsplit(url).query).get("sort", [])
        if not sort.startswith("-") and sort != "default"
    }
    refused = []
    for field in sorted(fields):
        sorted_links = Links()
        sorted_links.feed(admin_client.get(reverse(name), {"sort": field}).content.decode())
        if not any(parse_qs(urlsplit(url).query).get("sort") == [f"-{field}"] for url in sorted_links.urls):
            refused.append(field)
    assert not refused
