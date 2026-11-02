"""Ticketing admins can create tickets on a team's behalf (issue #39)."""

from unittest.mock import patch

import pytest
from django.test import Client
from django.urls import reverse

from team.models import Team
from ticketing.models import Ticket

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _quotient_never_asked():
    """The form reads the synced snapshot; a live Quotient call would hang it while Quotient is down."""
    with patch("quotient.client.get_quotient_client", side_effect=AssertionError("the ticket form asked Quotient")):
        yield


@pytest.fixture
def teams(blue_team):
    return (
        blue_team,
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


def test_the_team_picker_cleans_to_the_picked_team(teams):
    from ticketing.forms import CreateTicketForm

    form = CreateTicketForm(
        {"title": "x", "category": 6, "description": "x", "team_id": str(teams[1].id)},
        staff=True,
        teams=Team.objects.filter(is_active=True),
    )

    assert form.is_valid(), form.errors
    assert form.cleaned_data["team_id"] == teams[1]


def test_an_inactive_team_is_refused_beside_the_team_picker(ticketing_admin_user, teams):
    teams[1].is_active = False
    teams[1].save()

    html = _post(_client(ticketing_admin_user), team_id=teams[1].id).content.decode()

    assert '<p class="errornote" id="team_id_error">Please select a team.</p>' in html
    assert not Ticket.objects.exists()


def test_a_refused_ticket_names_the_picked_team(ticketing_admin_user, teams):
    response = _client(ticketing_admin_user).post(
        reverse("create_ticket"), {"category": 6, "description": "x", "team_id": teams[1].id}
    )

    assert '<p class="page-header-subtitle">Team 7</p>' in response.content.decode()


def test_without_a_team_picker_a_posted_team_is_ignored():
    from ticketing.forms import CreateTicketForm

    form = CreateTicketForm({"title": "x", "category": 6, "description": "x", "team_id": "not a team"})

    assert form.is_valid(), form.errors
    assert form.cleaned_data["team_id"] is None
