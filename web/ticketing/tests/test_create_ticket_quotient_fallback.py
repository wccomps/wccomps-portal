"""Ticket creation keeps working with no synced Quotient metadata, and keeps the last synced lists while Quotient
is down."""

from unittest.mock import patch

import pytest
from django.test import Client
from django.urls import reverse
from scoring.models import QuotientMetadataCache

from ticketing.models import Ticket, TicketCategory

pytestmark = pytest.mark.django_db

UNAVAILABLE_NOTICE = "Scoring engine is unavailable"


@pytest.fixture(autouse=True)
def _quotient_down():
    with patch("quotient.client.get_quotient_client", side_effect=AssertionError("the ticket form asked Quotient")):
        yield


def _synced(boxes: dict[str, str], services: dict[str, list[str]]) -> None:
    QuotientMetadataCache.objects.create(
        boxes=[
            {"name": name, "ip": ip, "services": [{"name": svc, "display_name": svc} for svc in services.get(name, [])]}
            for name, ip in boxes.items()
        ]
    )


@pytest.fixture
def team(blue_team_user, blue_team):
    return blue_team


@pytest.fixture
def client(blue_team_user, team):
    c = Client()
    c.force_login(blue_team_user)
    return c


def test_nothing_synced_renders_text_inputs_and_notice(client):
    html = client.get(reverse("create_ticket")).content.decode()

    assert UNAVAILABLE_NOTICE in html
    assert 'type="text"' in html.split('name="hostname"')[0].rsplit("<input", 1)[1]
    assert 'type="text"' in html.split('name="service_name"')[0].rsplit("<input", 1)[1]
    assert '<select name="service_name"' not in html


def test_the_last_synced_lists_render_as_pickers_while_quotient_is_down(client):
    _synced({"web01": "10.0.1.5"}, {"web01": ["HTTP"]})

    html = client.get(reverse("create_ticket")).content.decode()

    assert UNAVAILABLE_NOTICE not in html
    assert 'data-value="web01"' in html
    assert '<select name="service_name"' in html
    assert "web01:HTTP" in html
    assert "web01: HTTP" in html


def test_scoring_ticket_can_be_filed_with_typed_service_when_unavailable(client, team):
    scoring_check = TicketCategory.objects.get(pk=3)
    assert "service_name" in (scoring_check.required_fields or [])

    response = client.post(
        reverse("create_ticket"),
        {"title": "HTTP down", "category": scoring_check.pk, "service_name": "web01 HTTP", "hostname": "web01"},
    )

    assert response.status_code == 302
    ticket = Ticket.objects.get(title="HTTP down")
    assert ticket.service_name == "web01 HTTP"
    assert ticket.team == team
