"""A team can always file an incident report: the affected IP is optional, as the model has it, and editable, so a
report without boxes still goes in."""

import re

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse

from scoring.models import IncidentReport
from team.models import Team

pytestmark = pytest.mark.django_db


@pytest.fixture
def client(blue_team_user: User, mock_quotient_client: object) -> Client:
    client = Client()
    client.force_login(blue_team_user)
    return client


def _report(**fields: str) -> dict[str, str]:
    return {
        "source_ip": "10.0.0.5",
        "attack_detected_at": "2026-01-01T10:00",
        "attack_description": "A shell on the web host",
        **fields,
    }


@pytest.mark.parametrize("destination_ip", ["10.0.1.20", ""])
def test_a_report_without_boxes_goes_in(client: Client, blue_team: Team, destination_ip: str) -> None:
    response = client.post(reverse("scoring:submit_incident_report"), _report(destination_ip=destination_ip))

    incident = IncidentReport.objects.get(team=blue_team)
    assert response.status_code == 302
    assert incident.destination_ip == (destination_ip or None)
    assert incident.affected_boxes == []


def test_the_affected_ip_can_be_typed_and_is_not_marked_required(client: Client, blue_team: Team) -> None:
    html = client.get(reverse("scoring:submit_incident_report")).content.decode()

    ip_input = re.search(r'<input[^>]*name="destination_ip"[^>]*>', html)
    assert ip_input
    assert "readonly" not in ip_input.group(0)
    assert not re.search(r'<label for="id_destination_ip"\s+class="required"', html)
