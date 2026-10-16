"""Django admin changelists whose columns render HTML."""

from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from scoring.models import IncidentReport, InjectScore, ServiceScore
from team.models import Team

pytestmark = pytest.mark.django_db


@pytest.fixture
def client_with_rows(admin_user: User) -> Client:
    team = Team.objects.create(team_number=31, team_name="Team 31")
    submitter = User.objects.create(username="blue31")
    IncidentReport.objects.create(
        team=team,
        submitted_by=submitter,
        attack_description="x",
        source_ip="10.0.0.1",
        attack_detected_at=timezone.now(),
    )
    InjectScore.objects.create(
        team=team, inject_id="1", inject_name="Memo", max_points=Decimal(10), points_awarded=Decimal(9)
    )
    ServiceScore.objects.create(team=team, service_points=Decimal(5), sla_violations=Decimal(-1))
    client = Client()
    client.force_login(admin_user)
    return client


@pytest.mark.parametrize("model", ["incidentreport", "injectscore", "servicescore"])
def test_changelist_renders(client_with_rows: Client, model: str) -> None:
    response = client_with_rows.get(reverse(f"admin:scoring_{model}_changelist"))
    assert response.status_code == 200
    assert b'<span style="color: ' in response.content
