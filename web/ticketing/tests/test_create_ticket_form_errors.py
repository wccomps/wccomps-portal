"""A refused ticket shows what was posted again, with each error beside its field."""

import re
from html import unescape
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from django.test import Client
from django.urls import reverse

from team.models import Team
from ticketing.models import Ticket

pytestmark = pytest.mark.django_db


def _quotient(boxes: dict[str, str]) -> MagicMock:
    client = MagicMock()
    client.get_infrastructure.return_value = (
        SimpleNamespace(boxes=[SimpleNamespace(name=name, ip=ip) for name, ip in boxes.items()]) if boxes else None
    )
    client.get_box_names.return_value = list(boxes)
    client.get_service_choices.return_value = []
    return client


@pytest.fixture(autouse=True)
def _no_quotient():
    with patch("quotient.client.get_quotient_client", return_value=_quotient({})):
        yield


def _client(user):
    client = Client()
    client.force_login(user)
    return client


def _field_error(html: str, field: str) -> str | None:
    """The error shown in a field's own row, under the id its widget's aria-describedby names."""
    match = re.search(rf'<p class="errornote" id="{field}_error">([^<]*)</p>', html)
    return unescape(match.group(1)) if match else None


def test_a_refused_ticket_shows_the_posted_values_and_the_error_beside_its_field(
    blue_team_user, blue_team, box_reset_category
):
    response = _client(blue_team_user).post(
        reverse("create_ticket"),
        {
            "title": "Web box won't boot",
            "category": box_reset_category.pk,
            "description": "Stuck at the boot loader",
            "hostname": "web01",
        },
    )
    html = response.content.decode()

    assert response.status_code == 200
    assert not Ticket.objects.exists()
    assert _field_error(html, "ip_address") == "IP Address is required for this category."
    assert _field_error(html, "hostname") is None
    assert 'value="Web box won&#x27;t boot"' in html
    assert "Stuck at the boot loader</textarea>" in html
    assert re.search(rf'<option value="{box_reset_category.pk}" selected>', html)
    assert re.search(r'name="hostname"[^>]* value="web01"|value="web01"[^>]* name="hostname"', html)


def test_a_missing_title_is_an_error_beside_the_title(blue_team_user, blue_team):
    html = _client(blue_team_user).post(reverse("create_ticket"), {"category": 6}).content.decode()

    assert _field_error(html, "title") == "This field is required."


def test_staff_who_pick_no_team_are_told_beside_the_team_picker(ticketing_admin_user, blue_team):
    other = Team.objects.create(team_number=7, team_name="Team 07")
    html = (
        _client(ticketing_admin_user)
        .post(reverse("create_ticket"), {"title": "For a team", "category": 6, "description": "x"})
        .content.decode()
    )

    assert _field_error(html, "team_id") == "Please select a team."
    assert f'<option value="{other.id}">Team 7</option>' in html


def test_the_box_picker_is_the_hostname_field(blue_team_user, blue_team):
    with patch("quotient.client.get_quotient_client", return_value=_quotient({"web01": "10.0.1.5"})):
        html = _client(blue_team_user).get(reverse("create_ticket")).content.decode()

    assert re.search(r"<legend[^>]*>Hostname:</legend>", html)
    assert re.search(r'<select name="hostname" hidden[^>]* id="hostname"', html)
    assert re.findall(r'<button type="button"\s+data-value="([^"]*)"', html) == ["web01"]


def test_a_refused_ticket_keeps_the_picked_box_pressed(blue_team_user, blue_team, box_reset_category):
    with patch(
        "quotient.client.get_quotient_client", return_value=_quotient({"web01": "10.0.1.5", "db01": "10.0.1.6"})
    ):
        html = (
            _client(blue_team_user)
            .post(reverse("create_ticket"), {"category": box_reset_category.pk, "hostname": "web01"})
            .content.decode()
        )

    assert re.search(r'data-value="web01"\s+aria-pressed="true"', html)
    assert re.search(r'data-value="db01"\s+aria-pressed="false"', html)
