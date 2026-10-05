"""The reviewer's assessment notes on an approved incident are shown on the report page."""

from decimal import Decimal

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from scoring.models import IncidentReport
from team.models import Team

pytestmark = pytest.mark.django_db


def test_approval_notes_shown_on_approved_incident(gold_team_user):
    team = Team.objects.create(team_number=1, team_name="T1", is_active=True)
    incident = IncidentReport.objects.create(
        team=team,
        submitted_by=gold_team_user,
        attack_description="Go binary beaconing over DNS",
        source_ip="10.0.0.9",
        attack_detected_at=timezone.now(),
        is_approved=True,
        points_returned=Decimal("40"),
        approval_notes="ATT&CK T1071 Command and Control; NIST 800-61 phase: Eradication.",
    )

    client = Client()
    client.force_login(gold_team_user)
    resp = client.get(reverse("scoring:view_incident_report", args=[incident.id]))

    assert resp.status_code == 200
    assert "NIST 800-61 phase: Eradication" in resp.content.decode()
