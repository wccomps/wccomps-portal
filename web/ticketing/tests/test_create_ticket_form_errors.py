"""A refused ticket shows what was posted again, with each error beside its field."""

import re
from unittest.mock import patch

import pytest
from django.test import Client
from django.urls import reverse
from scoring.models import QuotientMetadataCache

from core.tests.form_html import field_error
from team.models import Team
from ticketing.models import Ticket

pytestmark = pytest.mark.django_db


def _synced(boxes: dict[str, str]) -> None:
    QuotientMetadataCache.objects.create(boxes=[{"name": name, "ip": ip, "services": []} for name, ip in boxes.items()])


@pytest.fixture(autouse=True)
def _quotient_never_asked():
    """The form reads the synced snapshot; a live Quotient call would hang it while Quotient is down."""
    with patch("quotient.client.get_quotient_client", side_effect=AssertionError("the ticket form asked Quotient")):
        yield


def _client(user):
    client = Client()
    client.force_login(user)
    return client


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
    assert field_error(html, "ip_address") == "IP Address is required for this category."
    assert field_error(html, "hostname") is None
    assert 'value="Web box won&#x27;t boot"' in html
    assert "Stuck at the boot loader</textarea>" in html
    assert re.search(rf'<option value="{box_reset_category.pk}" selected>', html)
    assert re.search(r'name="hostname"[^>]* value="web01"|value="web01"[^>]* name="hostname"', html)


def test_a_missing_title_is_an_error_beside_the_title(blue_team_user, blue_team):
    html = _client(blue_team_user).post(reverse("create_ticket"), {"category": 6}).content.decode()

    assert field_error(html, "title") == "This field is required."


def test_staff_who_pick_no_team_are_told_beside_the_team_picker(ticketing_admin_user, blue_team):
    other = Team.objects.create(team_number=7, team_name="Team 07")
    html = (
        _client(ticketing_admin_user)
        .post(reverse("create_ticket"), {"title": "For a team", "category": 6, "description": "x"})
        .content.decode()
    )

    assert field_error(html, "team_id") == "Please select a team."
    assert f'<option value="{other.id}">Team 7</option>' in html


def test_the_box_picker_is_the_hostname_field(blue_team_user, blue_team):
    _synced({"web01": "10.0.1.5"})

    html = _client(blue_team_user).get(reverse("create_ticket")).content.decode()

    assert re.search(r"<legend[^>]*>Hostname:</legend>", html)
    assert re.search(r'<select name="hostname" hidden[^>]* id="hostname"', html)
    assert re.findall(r'<button type="button"\s+data-value="([^"]*)"', html) == ["web01"]


def test_a_refused_ticket_keeps_the_picked_box_pressed(blue_team_user, blue_team, box_reset_category):
    _synced({"web01": "10.0.1.5", "db01": "10.0.1.6"})

    html = (
        _client(blue_team_user)
        .post(reverse("create_ticket"), {"category": box_reset_category.pk, "hostname": "web01"})
        .content.decode()
    )

    assert re.search(r'data-value="web01"\s+aria-pressed="true"', html)
    assert re.search(r'data-value="db01"\s+aria-pressed="false"', html)


def test_a_category_option_reads_its_points_as_the_bot_does(blue_team_user, box_reset_category):
    box_reset_category.points = 0
    box_reset_category.save()

    html = _client(blue_team_user).get(reverse("create_ticket")).content.decode()

    assert f"{box_reset_category.display_name} ({box_reset_category.points_label})</option>" in html
    assert box_reset_category.points_label == "0pt"
