"""Ticketing admins can create tickets on a team's behalf (issue #39)."""

from unittest.mock import MagicMock, patch

import pytest
from django.test import Client
from django.urls import reverse

from team.models import Team
from ticketing.models import Ticket

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
def teams():
    return (
        Team.objects.update_or_create(team_number=1, defaults={"team_name": "Team 01"})[0],
        Team.objects.create(team_number=7, team_name="Team 07"),
    )


def _client(user):
    client = Client()
    client.force_login(user)
    return client


def _post(client, **extra):
    return client.post(
        reverse("create_ticket"),
        {"title": "Filed by staff", "category": 6, "description": "on behalf of the team", **extra},
    )


def test_ticketing_admin_sees_team_picker(ticketing_admin_user, teams):
    response = _client(ticketing_admin_user).get(reverse("create_ticket"))

    assert response.status_code == 200
    html = response.content.decode()
    assert 'name="team_id"' in html
    assert f'value="{teams[1].id}"' in html


def test_ticketing_admin_creates_ticket_for_chosen_team(ticketing_admin_user, teams):
    response = _post(_client(ticketing_admin_user), team_id=teams[1].id)

    assert response.status_code == 302
    ticket = Ticket.objects.get(title="Filed by staff")
    assert ticket.team == teams[1]


def test_ticketing_admin_must_choose_a_team(ticketing_admin_user, teams):
    response = _post(_client(ticketing_admin_user))

    assert b"Please select a team." in response.content
    assert not Ticket.objects.exists()


def test_ticketing_support_still_cannot_create(ticketing_support_user, teams):
    response = _client(ticketing_support_user).get(reverse("create_ticket"))

    assert b"You must be a team member to create tickets." in response.content


def test_blue_team_still_files_only_for_own_team(blue_team_user, teams):
    """A team member can't redirect a ticket to another team by posting team_id."""
    _post(_client(blue_team_user), team_id=teams[1].id)

    assert Ticket.objects.get(title="Filed by staff").team == teams[0]
