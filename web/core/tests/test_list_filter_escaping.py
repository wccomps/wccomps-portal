"""A list page's filters are escaped where the page echoes them, stay one parameter in the links that carry them
on, a filter that isn't a number is ignored rather than failing the page, and every sort header sorts."""

from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

import pytest
from django.contrib.auth.models import User
from django.db.models import Model
from django.test import Client
from django.urls import reverse
from hypothesis import example, given
from hypothesis import strategies as st

from core.tests.strategies import request_text, rolled_back
from core.tests.test_list_filters import list_url

pytestmark = pytest.mark.django_db

ECHOED_FILTERS = [
    ("ticket_list", "team"),
    ("ticket_list", "search"),
    ("ops_review_tickets", "search"),
    ("school_info", "search"),
    ("admin_teams", "search"),
    ("admin_categories", "search"),
    ("registration_review_list", "search"),
    ("registration_season_list", "search"),
    ("registration_event_list", "search"),
    ("scoring:red_team_scores", "attack_type"),
    ("scoring:red_team_findings", "submitter"),
    ("scoring:inject_grades_review", "inject"),
    ("scoring:review_inject_feedback", "search"),
    ("scoring:incident_list", "search"),
    ("scoring:review_incidents", "box"),
    ("scoring:review_orange", "search"),
    ("scoring:ip_pool_list", "search"),
    ("orange_team:review_queue", "search"),
    ("orange_team:check_list", "search"),
    ("packets_list", "search"),
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
        response = admin_client.get(list_url(name), {param: markup})
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
    response = admin_client.get(list_url(name), {param: value})
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
    ("scoring:review_inject_feedback", "team"),
    ("scoring:review_orange", "team"),
    ("scoring:review_orange", "check"),
    ("orange_team:review_queue", "team"),
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
    from datetime import time, timedelta
    from decimal import Decimal

    from django.utils import timezone
    from orange_team.models import OrangeAssignment, OrangeCheck
    from packets.models import Packet
    from registration.models import Event, RegistrationEventEnrollment, School, Season, TeamRegistration
    from scoring.models import AttackType, IncidentReport, InjectScore, OrangeTeamScore, RedTeamIPPool, RedTeamScore

    from team.models import DiscordLink, SchoolInfo, Team
    from ticketing.models import Ticket, TicketCategory

    now = timezone.now()
    categories = list(TicketCategory.objects.order_by("pk")[:3])
    teams: list[Team] = []
    regs: list[TeamRegistration] = []
    event_season = Season.objects.create(name="c season", year=2021)
    for i, (number, name) in enumerate([(3, "b"), (1, "c"), (2, "a")]):
        team = Team.objects.create(team_number=number, team_name=f"Team {number}")
        teams.append(team)
        when = now - timedelta(hours=i + 1)
        assignee = User.objects.create_user(username=f"{name}-assignee")
        school = School.for_name(f"{name} school")
        info = SchoolInfo.objects.create(
            team=team,
            school=school,
            contact_email=f"{name}@example.com",
            secondary_email=f"{name}-sec@example.com",
        )
        SchoolInfo.objects.filter(pk=info.pk).update(updated_at=when)
        for j in range(i):
            DiscordLink.objects.create(
                team=team,
                discord_id=number * 100 + j,
                discord_username=f"{name}{j}",
                user=User.objects.create_user(username=f"{name}-member-{j}"),
                is_active=True,
            )
        reg = TeamRegistration.objects.create(
            school_name=f"{name} school",
            status=("pending", "approved", "rejected")[i],
        )
        regs.append(reg)
        TeamRegistration.objects.filter(pk=reg.pk).update(submitted_at=when)
        season = event_season if number == 1 else Season.objects.create(name=f"{name} season", year=2020 + number)
        for j in range(i):
            Event.objects.create(
                season=season,
                name=f"{name} event {j}",
                event_type="invitational",
                event_number=j + 10,
                date=now.date() + timedelta(days=j),
            )
        main_event = Event.objects.create(
            season=event_season,
            name=f"{name} main event",
            event_type=("invitational", "qualifier", "regional")[i],
            event_number=number,
            date=now.date() + timedelta(days=number * 10),
            start_time=time(9 + number, 0),
        )
        for prev_reg in regs[:-1]:
            RegistrationEventEnrollment.objects.create(registration=prev_reg, event=main_event)
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
        pool = RedTeamIPPool.objects.create(
            name=f"{name} pool",
            ip_addresses=f"10.0.{number}.1",
            created_by=admin_user,
        )
        finding = RedTeamScore.objects.create(
            attack_type=AttackType.objects.create(name=f"{name} attack"),
            points_per_team=Decimal(number),
            submitted_by=admin_user,
            source_ip_pool=pool if i > 0 else None,
        )
        incident = IncidentReport.objects.create(
            team=team, submitted_by=admin_user, attack_description=name, source_ip="10.0.0.1", attack_detected_at=when
        )
        orange = OrangeTeamScore.objects.create(team=team, description=name, points_awarded=Decimal(number))
        check = OrangeCheck.objects.create(
            title=f"{name} check",
            description=name,
            scheduled_at=when,
            max_points=10,
        )
        OrangeAssignment.objects.create(
            orange_check=check,
            team=team,
            user=assignee,
            status=("submitted", "in_progress", "submitted")[i],
            score=number,
            submitted_at=when,
        )
        for prev_team in teams[:-1]:
            OrangeAssignment.objects.create(
                orange_check=check,
                team=prev_team,
                user=assignee,
                status="approved",
                score=1,
            )
        InjectScore.objects.create(
            team=team,
            inject_id=name,
            inject_name=f"{name} inject",
            max_points=Decimal(100),
            points_awarded=Decimal(number),
            graded_by=admin_user,
            graded_at=when,
        )
        InjectScore.objects.create(
            team=team,
            inject_id=f"{name}-fb",
            inject_name=f"{name} inject",
            max_points=Decimal(100),
            points_awarded=Decimal(number),
            graded_by=admin_user,
            graded_at=when,
            is_approved=True,
            feedback=name,
        )
        packet = Packet.objects.create(
            title=f"{name} packet",
            file_data=b"%PDF-1.4",
            filename=f"{name}.pdf",
            mime_type="application/pdf",
            file_size=8,
            status=("completed", "cancelled", "completed")[i],
            uploaded_by=admin_user.username,
        )
        for row in (ticket, pool, finding, incident, orange, packet):
            type(row).objects.filter(pk=row.pk).update(created_at=when)


ROW_KEYS = (
    "page_obj",
    "findings",
    "categories",
    "registrations",
    "seasons",
    "events",
    "scores",
    "incidents",
    "pools",
    "review_assignments",
    "checks",
    "history",
    "teams",
)


def sort_key(row: object, field: str) -> object:
    """The value a row sorts by for an order_by field, as the database orders it: a__b follows the relation, and a
    related row sorts by its model's default ordering, or its pk without one. The ticket pages list
    {"ticket": ticket, ...} rows; the event list wraps each event in an (event, ...) tuple."""
    value = row[0] if isinstance(row, tuple) else row["ticket"] if isinstance(row, dict) else row
    for part in field.split("__"):
        value = getattr(value, part)
    if not isinstance(value, Model):
        return value
    return tuple(getattr(value, name) for name in value._meta.ordering) or value.pk


@pytest.mark.parametrize("name", LIST_PAGES)
def test_every_sort_header_a_list_shows_sorts_its_rows(admin_client: Client, three_of_each: None, name: str) -> None:
    url = list_url(name)
    links = Links()
    links.feed(admin_client.get(url).content.decode())
    fields = {
        sort.removeprefix("-")
        for link_url in links.urls
        for sort in parse_qs(urlsplit(link_url).query).get("sort", [])
        if sort != "default"
    }
    assert fields
    unsorted = []
    for field in sorted(fields):
        for direction in ("", "-"):
            context = admin_client.get(url, {"sort": direction + field}).context
            rows = list(next(context[k] for k in ROW_KEYS if k in context))
            keys = [sort_key(row, field) for row in rows]
            if len(set(keys)) < 2 or keys != sorted(keys, reverse=direction == "-"):
                unsorted.append(f"{direction}{field}: {keys}")
    assert not unsorted


@pytest.mark.parametrize("name", LIST_PAGES)
def test_an_empty_list_blames_filters_only_when_some_are_set(admin_client: Client, name: str) -> None:
    def says_filtered(query: dict[str, str]) -> bool:
        page = admin_client.get(list_url(name), query).content.decode()
        return "match your filters" in page or "match the current filters" in page or "the selected status" in page

    assert not says_filtered({})
    assert not says_filtered({"team": "", "search": "  "})
    assert says_filtered({"search": "nothing matches this"})
