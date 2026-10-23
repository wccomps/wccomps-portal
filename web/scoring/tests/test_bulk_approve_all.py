"""Every scoring bulk-approve view records who approved each row and when."""

from decimal import Decimal

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from scoring.models import IncidentReport, InjectScore, OrangeTeamScore, RedTeamScore
from team.models import Team

pytestmark = pytest.mark.django_db


def _red(team: Team) -> RedTeamScore:
    finding = RedTeamScore.objects.create(points_per_team=Decimal("10"))
    finding.affected_teams.set([team])
    return finding


def _incident(team: Team) -> IncidentReport:
    return IncidentReport.objects.create(
        team=team, attack_description="x", source_ip="10.0.0.1", attack_detected_at=timezone.now()
    )


def _inject(team: Team) -> InjectScore:
    return InjectScore.objects.create(team=team, inject_id="i1", inject_name="I1", points_awarded=Decimal("5"))


def _orange(team: Team) -> OrangeTeamScore:
    return OrangeTeamScore.objects.create(team=team, description="x", points_awarded=Decimal("5"))


@pytest.mark.parametrize(
    ("url_name", "field_name", "make"),
    [
        ("scoring:bulk_approve_red_scores", "finding_ids", _red),
        ("scoring:bulk_approve_incidents", "incident_ids", _incident),
        ("scoring:inject_grades_bulk_approve", "grade_ids", _inject),
        ("scoring:bulk_approve_orange_adjustments", "adjustment_ids", _orange),
    ],
)
def test_bulk_approve_records_approver(gold_team_user, url_name, field_name, make):
    team = Team.objects.create(team_number=1, team_name="Team 1")
    row = make(team)
    client = Client()
    client.force_login(gold_team_user)

    client.post(reverse(url_name), {field_name: [row.pk]})

    row.refresh_from_db()
    assert row.is_approved
    assert row.approved_by == gold_team_user
    assert row.approved_at is not None
