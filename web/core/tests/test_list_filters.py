"""The list pages' filters: one approval-status filter and one team filter shared by the views and the templates, a
team filter that means a team number on every page, one wording for a list its filters empty, and no filters.<key>
in a template that no list filters by."""

import ast
import re
from datetime import timedelta
from decimal import Decimal
from html.parser import HTMLParser

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from core.tests.guards import REPO_DIR, fail_with, template_name, text, tracked_files, tracked_templates
from team.models import Team

pytestmark = pytest.mark.django_db

APPROVAL_LISTS = [
    "ops_review_tickets",
    "scoring:review_incidents",
    "scoring:review_orange",
    "scoring:inject_grades_review",
    "scoring:review_inject_feedback",
    "scoring:red_team_findings",
]
TEAM_LISTS = [
    "ticket_list",
    "ops_review_tickets",
    "scoring:review_incidents",
    "scoring:review_orange",
    "scoring:inject_grades_review",
    "scoring:red_team_findings",
]
FILTERED_LISTS = [*dict.fromkeys([*APPROVAL_LISTS, *TEAM_LISTS]), "registration_review_list"]


class Selects(HTMLParser):
    """Each <select>'s options by its name: (value, label)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.options: dict[str, list[tuple[str, str]]] = {}
        self.select = ""
        self.value: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "select":
            self.select = dict(attrs).get("name") or ""
            self.options[self.select] = []
        elif tag == "option":
            self.value = dict(attrs).get("value") or ""

    def handle_data(self, data: str) -> None:
        if self.value is not None and data.strip():
            self.options[self.select].append((self.value, " ".join(data.split())))
            self.value = None


def selects(client: Client, url: str) -> dict[str, list[tuple[str, str]]]:
    parser = Selects()
    parser.feed(client.get(url).content.decode())
    return parser.options


@pytest.fixture
def client(admin_user: User) -> Client:
    client = Client()
    client.force_login(admin_user)
    return client


def _rows_for(team: Team, marker: str, submitter: User) -> None:
    """One row of team's on every list with a team filter, each showing marker."""
    from orange_team.models import OrangeCheck
    from scoring.models import IncidentReport, InjectScore, OrangeTeamScore, RedTeamScore

    from ticketing.models import Ticket, TicketCategory

    category = TicketCategory.objects.get(pk=6)
    for status in ("open", "resolved"):
        Ticket.objects.create(
            ticket_number=f"{marker}-{status}", team=team, category=category, title=marker, status=status
        )
    IncidentReport.objects.create(
        team=team,
        attack_description=marker,
        source_ip="10.0.0.1",
        attack_detected_at=timezone.now() - timedelta(minutes=5),
    )
    check = OrangeCheck.objects.create(title=f"Check {marker}", description="")
    OrangeTeamScore.objects.create(team=team, orange_check=check, description=marker, points_awarded=Decimal(1))
    InjectScore.objects.create(
        team=team, inject_id=marker, inject_name=marker, max_points=Decimal(10), points_awarded=Decimal(5)
    )
    finding = RedTeamScore.objects.create(
        affected_service=marker, notes=marker, source_ip="10.0.0.1", points_per_team=Decimal(1), submitted_by=submitter
    )
    finding.affected_teams.add(team)


SELECT_RE = re.compile(r"<select.*?</select>", re.DOTALL)  # the filters' options name every row's check and inject


@pytest.mark.parametrize("name", TEAM_LISTS)
def test_the_team_filter_takes_a_team_number(client: Client, admin_user: User, name: str) -> None:
    """?team= named a team's id on four lists and its number on two."""
    Team.objects.bulk_create(Team(team_number=n, team_name=f"Team {n:02d}") for n in (40, 41, 42))
    seven = Team.objects.create(team_number=7, team_name="Team 07")
    other = Team.objects.create(team_number=8, team_name="Team 08")
    assert seven.pk != 7
    _rows_for(seven, "seven-row", admin_user)
    _rows_for(other, "eight-row", admin_user)

    page = SELECT_RE.sub("", client.get(reverse(name), {"team": "7", "status": "all"}).content.decode())

    assert "seven-row" in page
    assert "eight-row" not in page
    assert ("7", "Team 7") in selects(client, reverse(name))["team"]


@pytest.mark.parametrize("name", TEAM_LISTS)
def test_the_team_chosen_stays_chosen_with_no_rows(client: Client, name: str) -> None:
    """The select listed only teams with rows, so a team with none read "All Teams" while the list was filtered."""
    Team.objects.create(team_number=9, team_name="Team 09")
    page = client.get(reverse(name), {"team": "9"}).content.decode()
    assert re.search(r'<option value="9"\s+selected>\s*Team 9\s*</option>', page), name


@pytest.mark.parametrize("name", TEAM_LISTS)
@pytest.mark.parametrize("written", ["07", "+7", " 7 "])
def test_a_team_number_written_another_way_shows_as_chosen(client: Client, name: str, written: str) -> None:
    """?team=07 filtered on team 7 while the select read "All Teams": the option compared the text as written."""
    Team.objects.create(team_number=7, team_name="Team 07")
    page = client.get(reverse(name), {"team": written}).content.decode()
    assert re.search(r'<option value="7"\s+selected>\s*Team 7\s*</option>', page), name


@pytest.mark.parametrize("name", TEAM_LISTS)
def test_a_team_number_no_team_has_adds_no_option(client: Client, name: str) -> None:
    """?team=-3 added a "Team -3" option."""
    page = client.get(reverse(name), {"team": "-3"}).content.decode()
    assert "Team -3" not in page
    assert 'value="-3"' not in page


@pytest.mark.parametrize("name", APPROVAL_LISTS)
def test_every_approval_filter_offers_the_same_statuses(client: Client, name: str) -> None:
    assert selects(client, reverse(name))["status"] == [
        ("pending", "Pending Only"),
        ("approved", "Approved Only"),
        ("all", "All"),
    ]


@pytest.mark.parametrize("name", FILTERED_LISTS)
def test_a_list_its_filters_empty_says_so_in_one_wording(client: Client, name: str) -> None:
    query = {"search": "no-such-row", "status": "all"}
    if name == "registration_review_list":
        query = {"status": "rejected"}
    if name == "scoring:review_inject_feedback":
        query = {"inject": "no-such-inject"}
    page = client.get(reverse(name), query).content.decode()
    assert re.search(r"No \w+( \w+)* match your filters", page), name
    assert re.search(rf'<a href="{re.escape(reverse(name))}"[^>]*>\s*clearing all filters\s*</a>', page), name


def _filter_keys() -> set[str]:
    """Every filter a list page reads: the names in each list_filters(...) call's dicts."""
    keys: set[str] = set()
    for name in tracked_files():
        if name.startswith("web/") and name.endswith(".py") and "/tests/" not in name:
            for node in ast.walk(ast.parse((REPO_DIR / name).read_text(encoding="utf-8"))):
                if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "list_filters":
                    for inner in ast.walk(node):
                        if isinstance(inner, ast.Dict):
                            keys |= {k.value for k in inner.keys if isinstance(k, ast.Constant)}
    return keys


FILTERS_KEY_RE = re.compile(r"\bfilters\.(\w+)")
FILTERS_KEY_EXCEPTIONS: dict[str, str] = {}


def test_a_template_reads_only_filters_a_list_has() -> None:
    """A misspelled filters.<key> rendered blank: the filter looked unset and the page said nothing."""
    keys = _filter_keys()
    found = {
        f"{template_name(path)}:{key}": ""
        for path in tracked_templates()
        for key in FILTERS_KEY_RE.findall(text(path))
        if key not in keys
    }
    fail_with("filters.<key> no list filters by", found, FILTERS_KEY_EXCEPTIONS)
