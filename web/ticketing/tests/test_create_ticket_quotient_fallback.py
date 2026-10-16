"""Ticket creation keeps working when Quotient (source of hostnames/services) is unavailable."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from django.test import Client
from django.urls import reverse

from team.models import Team
from ticketing.models import Ticket, TicketCategory

pytestmark = pytest.mark.django_db

UNAVAILABLE_NOTICE = "Scoring engine is unavailable"


def _quotient(boxes: dict[str, str], services: list[dict[str, str]]) -> MagicMock:
    client = MagicMock()
    client.get_infrastructure.return_value = (
        SimpleNamespace(boxes=[SimpleNamespace(name=n, ip=ip) for n, ip in boxes.items()]) if boxes else None
    )
    client.get_box_names.return_value = list(boxes)
    client.get_service_choices.return_value = services
    return client


@pytest.fixture
def team(blue_team_user):
    return Team.objects.update_or_create(team_number=1, defaults={"team_name": "Blue Team 01"})[0]


@pytest.fixture
def client(blue_team_user, team):
    c = Client()
    c.force_login(blue_team_user)
    return c


def test_unavailable_renders_text_inputs_and_notice(client):
    with patch("quotient.client.get_quotient_client", return_value=_quotient({}, [])):
        html = client.get(reverse("create_ticket")).content.decode()

    assert UNAVAILABLE_NOTICE in html
    assert 'type="text"' in html.split('name="hostname"')[0].rsplit("<input", 1)[1]
    assert 'type="text"' in html.split('name="service_name"')[0].rsplit("<input", 1)[1]
    assert '<select name="service_name"' not in html


def test_available_renders_pickers_without_notice(client):
    services = [{"value": "web01:HTTP", "label": "web01 - HTTP", "box_name": "web01", "service_name": "HTTP"}]
    with patch("quotient.client.get_quotient_client", return_value=_quotient({"web01": "10.0.1.5"}, services)):
        html = client.get(reverse("create_ticket")).content.decode()

    assert UNAVAILABLE_NOTICE not in html
    assert 'data-hostname="web01"' in html
    assert '<select name="service_name"' in html


def test_scoring_ticket_can_be_filed_with_typed_service_when_unavailable(client, team):
    scoring_check = TicketCategory.objects.get(pk=3)
    assert "service_name" in (scoring_check.required_fields or [])

    with patch("quotient.client.get_quotient_client", return_value=_quotient({}, [])):
        response = client.post(
            reverse("create_ticket"),
            {"title": "HTTP down", "category": scoring_check.pk, "service_name": "web01 HTTP", "hostname": "web01"},
        )

    assert response.status_code == 302
    ticket = Ticket.objects.get(title="HTTP down")
    assert ticket.service_name == "web01 HTTP"
    assert ticket.team == team
