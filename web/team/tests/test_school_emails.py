"""Tests for school email property extraction on Team and SchoolInfo."""

from decimal import Decimal

import pytest
from scoring.calculator import Standing
from scoring.forms import ScorecardEmailForm

from team.models import SchoolInfo, Team

pytestmark = pytest.mark.django_db


def test_school_info_emails_both():
    team = Team.objects.create(team_number=1, team_name="Team 01")
    info = SchoolInfo.objects.create(
        team=team,
        school_name="Example University",
        contact_email="primary@example.edu",
        secondary_email="secondary@example.edu",
    )
    assert info.emails == ["primary@example.edu", "secondary@example.edu"]
    assert team.school_emails == ["primary@example.edu", "secondary@example.edu"]


def test_school_info_emails_primary_only():
    team = Team.objects.create(team_number=2, team_name="Team 02")
    info = SchoolInfo.objects.create(
        team=team,
        school_name="Example University",
        contact_email="primary@example.edu",
        secondary_email="",
    )
    assert info.emails == ["primary@example.edu"]
    assert team.school_emails == ["primary@example.edu"]


def test_team_without_school_info():
    team = Team.objects.create(team_number=3, team_name="Team 03")
    assert team.school_emails == []


def test_standing_school_emails():
    team = Team.objects.create(team_number=4, team_name="Team 04")
    SchoolInfo.objects.create(
        team=team,
        school_name="Example College",
        contact_email="coach@example.edu",
    )
    standing = Standing(
        team=team,
        is_excluded=False,
        service_points=Decimal(0),
        inject_points=Decimal(0),
        orange_points=Decimal(0),
        red_deductions=Decimal(0),
        sla_penalties=Decimal(0),
        point_adjustments=Decimal(0),
        incident_recovery_points=Decimal(0),
        total_score=Decimal(0),
    )
    assert standing.school_emails == ["coach@example.edu"]


def test_scorecard_email_form_get_custom_message():
    form_empty = ScorecardEmailForm({})
    assert form_empty.get_custom_message() == ""

    form_filled = ScorecardEmailForm({"custom_message": "  Please review findings.  "})
    assert form_filled.get_custom_message() == "Please review findings."
