"""Teams create tickets only in user-creatable categories; staff may use any."""

from unittest.mock import MagicMock, patch

import pytest
from django.test import Client
from django.urls import reverse

from ticketing.models import Ticket, TicketCategory

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _no_quotient():
    client = MagicMock()
    client.get_infrastructure.return_value = None
    client.get_box_names.return_value = []
    client.get_service_choices.return_value = []
    with patch("quotient.client.get_quotient_client", return_value=client):
        yield


@pytest.fixture
def staff_only():
    return TicketCategory.objects.create(
        pk=95, display_name="Staff Only", points=0, required_fields=[], user_creatable=False, sort_order=95
    )


@pytest.fixture
def team_1(blue_team):
    return blue_team


def _client(user):
    client = Client()
    client.force_login(user)
    return client


def test_team_form_hides_staff_only_category(blue_team_user, team_1, staff_only):
    html = _client(blue_team_user).get(reverse("create_ticket")).content.decode()
    assert "Staff Only" not in html


def test_team_cannot_post_staff_only_category(blue_team_user, team_1, staff_only):
    response = _client(blue_team_user).post(
        reverse("create_ticket"), {"title": "x", "category": staff_only.pk, "description": "x"}
    )
    assert "Invalid ticket category selected." in response.content.decode()
    assert not Ticket.objects.exists()


def test_staff_can_use_staff_only_category(ticketing_admin_user, team_1, staff_only):
    response = _client(ticketing_admin_user).post(
        reverse("create_ticket"), {"title": "x", "category": staff_only.pk, "description": "x", "team_id": team_1.id}
    )
    assert response.status_code == 302
    assert Ticket.objects.get().category == staff_only


def test_box_reset_keeps_the_teams_description_and_the_hostname(blue_team_user, team_1, box_reset_category):
    response = _client(blue_team_user).post(
        reverse("create_ticket"),
        {
            "title": "Web box won't boot",
            "category": box_reset_category.pk,
            "description": "Stuck at the boot loader since the last round",
            "hostname": "web01",
            "ip_address": "10.0.1.5",
        },
    )

    assert response.status_code == 302
    ticket = Ticket.objects.get()
    assert ticket.description == "Stuck at the boot loader since the last round"
    assert ticket.hostname == "web01"
