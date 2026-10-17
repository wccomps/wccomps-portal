"""Every page whose placeholders became blank renders readable text when its values are missing: no Python "None",
and parentheses that close. The rows are sparse on purpose: unscored assignments, findings without an attack type,
vector or boxes, a team without school info, a packet nobody has downloaded."""

import re
from collections.abc import Iterable
from decimal import Decimal
from html.parser import HTMLParser

import pytest
from browser_tests.conftest import resolve_url
from browser_tests.page_registry import PAGES
from browser_tests.table_data import seed_table_data
from django.contrib.auth.models import User
from django.template.loader import render_to_string
from django.test import Client, RequestFactory
from django.urls import reverse
from django.utils import timezone

from core.tests.guards import fail_with
from core.tests.roles import role_user

pytestmark = pytest.mark.django_db


class VisibleText(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if not self.cdata_elem:
            self.parts.append(data)


def visible_text(html: str) -> str:
    parser = VisibleText()
    parser.feed(html)
    return " ".join(" ".join(parser.parts).split())


def unreadable(html: str) -> list[str]:
    """What makes the page's text unreadable: a Python "None", or parentheses that don't close."""
    words = visible_text(html)
    return [
        problem
        for problem, found in (
            ("None", re.search(r"\bNone\b", words)),
            ("parentheses", words.count("(") != words.count(")")),
        )
        if found
    ]


def assert_readable(html: str, where: str) -> None:
    assert not unreadable(html), f"{where}: {visible_text(html)}"


# "<page>:<problem>" -> why the page reads that way on purpose
UNREADABLE_EXCEPTIONS = {
    "/admin/scoring/attacktype/:parentheses": "the attack type admin's Description column cuts a long description at "
    "50 characters, closing parenthesis and all (the seeded attack types)",
}


def read_every(client: Client, pages: Iterable[str]) -> dict[str, str]:
    found = {}
    for page in pages:
        response = client.get(page)
        assert response.status_code == 200, page
        found |= dict.fromkeys((f"{page}:{problem}" for problem in unreadable(response.content.decode())), "")
    return found


@pytest.fixture
def rows(admin_user: User) -> tuple[dict, dict]:
    """The seeded rows, and the same with sparse rows in place of those the registry's URLs name."""
    from orange_team.models import OrangeAssignment, OrangeCheck, OrangeFollowUp
    from scoring.models import IncidentReport, RedTeamScore

    from team.models import Team

    data = seed_table_data(admin_user)
    team = Team.objects.create(team_number=7, team_name="Team 07")  # no school info
    check = OrangeCheck.objects.create(title="Unscheduled", description="")
    assignment = OrangeAssignment.objects.create(orange_check=check, user=admin_user, team=team, status="in_progress")
    OrangeFollowUp.objects.create(user=admin_user, assignment=assignment, remind_at=timezone.now())
    finding = RedTeamScore.objects.create(
        attack_vector="", source_ip="10.0.0.9", points_per_team=Decimal(5), submitted_by=admin_user
    )
    finding.affected_teams.add(team)
    incident = IncidentReport.objects.create(
        team=team,
        submitted_by=admin_user,
        attack_description="x",
        source_ip="10.0.0.9",
        attack_detected_at=timezone.now(),
    )
    return data, {**data, "team": team, "orange_check": check, "finding": finding, "incident": incident}


def _pages_as(role: str, data: dict) -> list[str]:
    """Each browser registry page the role is rendered as there, its URL naming the rows in data."""
    return [resolve_url(page, data) for page in PAGES if role in page.allowed_roles and not page.expect_redirect]


def test_staff_pages_read_without_their_missing_values(admin_user: User, rows: tuple[dict, dict]) -> None:
    from django.contrib import admin

    seeded, sparse = rows
    client = Client()
    client.force_login(admin_user)
    changelists = [reverse(f"admin:{m._meta.app_label}_{m._meta.model_name}_changelist") for m in admin.site._registry]
    pages = {*_pages_as("admin", seeded), *_pages_as("admin", sparse), *changelists}
    fail_with("Staff pages with unreadable text", read_every(client, sorted(pages)), UNREADABLE_EXCEPTIONS)


def test_team_pages_read_without_their_missing_values(rows: tuple[dict, dict]) -> None:
    client = Client()
    client.force_login(role_user("blue_team"))
    fail_with("Team pages with unreadable text", read_every(client, _pages_as("blue_team", rows[0])), {})


def test_the_printed_scorecard_and_import_preview_read_without_missing_values(
    admin_user: User, rows: tuple[dict, dict]
) -> None:
    from scoring.views.leaderboard import _scorecard_context

    for team_number in (1, 7):
        assert_readable(render_to_string("scoring/scorecard_print.html", _scorecard_context(team_number)), "print")
    row = {"team_number": 7, "school_name": "U", "returning_events": None, "contact_email": "a@u.edu"}
    rows = [{**row, "secondary_email": None, "notes": None}]
    preview = {"teams_to_create": rows, "token": "t", "can_import": True, "event_label": "E", "mode_label": "Add"}
    request = RequestFactory().get("/")
    request.user = admin_user
    page = render_to_string("school_info_import.html", {"preview_data": preview}, request=request)
    assert_readable(page, "import preview")
