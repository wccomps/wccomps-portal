"""Matching an incident report to a suggested red team finding."""

from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from scoring.models import IncidentReport, RedTeamScore
from team.models import Team

pytestmark = pytest.mark.django_db


def test_match_to_suggested_finding_saves(gold_team_user: User) -> None:
    team = Team.objects.create(team_number=33, team_name="Team 33")
    blue = User.objects.create(username="blue33")
    incident = IncidentReport.objects.create(
        team=team, submitted_by=blue, attack_description="x", source_ip="10.0.0.5", attack_detected_at=timezone.now()
    )
    finding = RedTeamScore.objects.create(
        attack_vector="SQLi", source_ip="10.0.0.5", points_per_team=Decimal("-50"), submitted_by=gold_team_user
    )
    finding.affected_teams.add(team)

    client = Client()
    client.force_login(gold_team_user)
    response = client.post(
        reverse("scoring:match_incident", args=[incident.id]),
        {"matched_to_red_score": finding.id, "points_returned": "40", "approval_notes": ""},
    )

    assert response.status_code == 302
    incident.refresh_from_db()
    assert incident.matched_to_red_score_id == finding.id
    assert incident.is_approved


def test_matching_already_approved_incident_is_refused(gold_team_user: User) -> None:
    team = Team.objects.create(team_number=34, team_name="Team 34")
    blue = User.objects.create(username="blue34")
    incident = IncidentReport.objects.create(
        team=team,
        submitted_by=blue,
        attack_description="x",
        source_ip="10.0.0.5",
        attack_detected_at=timezone.now(),
        points_returned=Decimal("25"),
    )
    incident.approve(gold_team_user)
    finding = RedTeamScore.objects.create(
        source_ip="10.0.0.5", points_per_team=Decimal("-50"), submitted_by=gold_team_user
    )
    finding.affected_teams.add(team)

    client = Client()
    client.force_login(gold_team_user)
    response = client.post(
        reverse("scoring:match_incident", args=[incident.id]),
        {"matched_to_red_score": finding.id, "points_returned": "40", "approval_notes": ""},
    )

    assert response.status_code == 302
    incident.refresh_from_db()
    assert incident.matched_to_red_score_id is None
    assert incident.points_returned == Decimal("25")
