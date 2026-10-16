"""Every page whose placeholders became blank renders readable text when its values are missing: no Python "None",
and parentheses that close. The rows are sparse on purpose: unscored assignments, findings without an attack type,
vector or boxes, a team without school info, a packet nobody has downloaded."""

import re
from decimal import Decimal
from html.parser import HTMLParser

import pytest
from browser_tests.table_data import seed_table_data
from django.contrib.auth.models import User
from django.template.loader import render_to_string
from django.test import Client, RequestFactory
from django.urls import reverse
from django.utils import timezone

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


def assert_readable(html: str, where: str) -> None:
    words = visible_text(html)
    assert not re.search(r"\bNone\b", words), f"{where}: {words}"
    assert words.count("(") == words.count(")"), f"{where}: {words}"


@pytest.fixture
def sparse(admin_user: User) -> dict:
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
    return {**data, "check": check, "finding": finding, "incident": incident}


def test_staff_pages_read_without_their_missing_values(admin_user: User, sparse: dict) -> None:
    User.objects.filter(pk=admin_user.pk).update(is_staff=True, is_superuser=True)  # Django admin's changelists
    client = Client()
    client.force_login(admin_user)
    pages = [
        reverse("orange_team:dashboard"),
        reverse("orange_team:review_queue"),
        reverse("orange_team:check_list"),
        reverse("orange_team:check_detail", args=[sparse["orange_check"].pk]),
        reverse("orange_team:check_detail", args=[sparse["check"].pk]),
        reverse("packet_detail", args=[sparse["packet"].pk]),
        reverse("school_info"),
        reverse("scoring:scorecard", args=[1]),
        reverse("scoring:scorecard", args=[7]),
        reverse("scoring:email_scorecards"),
        reverse("scoring:export_index"),
        reverse("scoring:match_incident", args=[sparse["incident"].pk]),
        reverse("scoring:view_incident_report", args=[sparse["incident"].pk]),
        reverse("scoring:view_red_score", args=[sparse["finding"].pk]),
        reverse("scoring:red_team_findings"),
        reverse("scoring:inject_grades_review"),
        reverse("ops_review_tickets"),
        reverse("ticket_detail", args=[sparse["ticket"].ticket_number]),
        reverse("admin:scoring_incidentreport_changelist"),
        reverse("admin:scoring_redteamscore_changelist"),
    ]
    for page in pages:
        response = client.get(page)
        assert response.status_code == 200, page
        assert_readable(response.content.decode(), page)


def test_team_pages_read_without_their_missing_values(sparse: dict) -> None:
    client = Client()
    client.force_login(role_user("blue_team"))
    for page in (reverse("ticket_list"), reverse("team_packet")):
        response = client.get(page)
        assert response.status_code == 200, page
        assert_readable(response.content.decode(), page)


def test_the_printed_scorecard_and_import_preview_read_without_missing_values(admin_user: User, sparse: dict) -> None:
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
