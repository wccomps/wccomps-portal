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


@pytest.fixture
def three_of_each(admin_user: User) -> None:
    """Three rows for every list page, differing in every column the pages sort by."""
    from datetime import timedelta
    from decimal import Decimal

    from django.utils import timezone
    from scoring.models import AttackType, IncidentReport, InjectScore, OrangeTeamScore, RedTeamScore

    from team.models import Team
    from ticketing.models import Ticket, TicketCategory

    now = timezone.now()
    categories = list(TicketCategory.objects.order_by("pk")[:3])
    for i, (number, name) in enumerate([(3, "b"), (1, "c"), (2, "a")]):
        team = Team.objects.create(team_number=number, team_name=f"Team {number}")
        when = now - timedelta(hours=i + 1)
        assignee = User.objects.create_user(username=f"{name}-assignee")
        ticket = Ticket.objects.create(
            ticket_number=f"T{number:03d}-001",
            team=team,
            category=categories[i],
            title=name,
            status="resolved",
            assigned_to=assignee,
            resolved_at=when,
            points_charged=10 * number,
        )
        Ticket.objects.create(
            ticket_number=f"T{number:03d}-002",
            team=team,
            category=categories[i],
            title=name,
            status=("open", "claimed", "cancelled")[i],
            assigned_to=assignee,
        )
        finding = RedTeamScore.objects.create(
            attack_vector=name,
            attack_type=AttackType.objects.create(name=f"{name} attack"),
            points_per_team=Decimal(number),
            submitted_by=admin_user,
        )
        incident = IncidentReport.objects.create(
            team=team, submitted_by=admin_user, attack_description=name, source_ip="10.0.0.1", attack_detected_at=when
        )
        orange = OrangeTeamScore.objects.create(team=team, description=name, points_awarded=Decimal(number))
        InjectScore.objects.create(
            team=team,
            inject_id=name,
            inject_name=f"{name} inject",
            max_points=Decimal(100),
            points_awarded=Decimal(number),
            graded_by=admin_user,
            graded_at=when,
        )
        for row in (ticket, finding, incident, orange):
            type(row).objects.filter(pk=row.pk).update(created_at=when)


def sort_key(row: object, field: str) -> object:
    """The value a row sorts by for an order_by field: a__b follows the relation; a related row sorts by its pk.
    The ticket pages list {"ticket": ticket, ...} rows."""
    value = row["ticket"] if isinstance(row, dict) else row
    for part in field.split("__"):
        value = getattr(value, part)
    return getattr(value, "pk", value)


@pytest.mark.parametrize("name", LIST_PAGES)
def test_every_sort_header_a_list_shows_sorts_its_rows(admin_client: Client, three_of_each: None, name: str) -> None:
    """Each header's field, asked for in either direction, orders the page's rows by it."""
    links = Links()
    links.feed(admin_client.get(reverse(name)).content.decode())
    fields = {
        sort.removeprefix("-")
        for url in links.urls
        for sort in parse_qs(urlsplit(url).query).get("sort", [])
        if sort != "default"
    }
    assert fields
    unsorted = []
    for field in sorted(fields):
        for direction in ("", "-"):
            rows = list(admin_client.get(reverse(name), {"sort": direction + field}).context["page_obj"])
            keys = [sort_key(row, field) for row in rows]
            if len(set(keys)) < 2 or keys != sorted(keys, reverse=direction == "-"):
                unsorted.append(f"{direction}{field}: {keys}")
    assert not unsorted
