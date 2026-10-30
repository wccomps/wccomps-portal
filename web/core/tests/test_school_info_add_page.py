"""The Add school page: one school joins the open event on the lowest free team."""

from datetime import date

import pytest
from django.test import Client
from django.urls import reverse
from registration.models import Event, EventTeamAssignment, Season
from registration.services import set_team_school

from team.models import SchoolInfo, Team

pytestmark = pytest.mark.django_db


@pytest.fixture
def client(gold_team_user):
    client = Client()
    client.force_login(gold_team_user)
    return client


@pytest.fixture
def event():
    season = Season.objects.create(year=2026, name="2026", is_active=True)
    return Event.objects.create(
        season=season, name="Invitational", event_type="invitational", date=date(2026, 10, 3), is_active=True
    )


@pytest.fixture
def teams():
    return [Team.objects.create(team_number=n, team_name=f"Team {n:02d}", is_active=False) for n in range(1, 4)]


def _add(client, name="Example University", **extra):
    return client.post(reverse("school_info_add"), {"school_name": name, "contact_email": "coach@example.edu", **extra})


def test_adds_the_school_on_the_lowest_free_team(client, gold_team_user, event, teams):
    set_team_school(teams[0], "Held College", contact_email="a@example.edu", updated_by="x")

    response = _add(client, secondary_email="alt@example.edu", notes="late")

    assert response.status_code == 302
    assert response.url == reverse("school_info")
    info = SchoolInfo.objects.get(school__name="Example University")
    assert info.team.team_number == 2
    assert info.team.is_active
    assert (info.secondary_email, info.notes, info.updated_by) == ("alt@example.edu", "late", gold_team_user.username)
    assert EventTeamAssignment.objects.get(event=event, team=info.team).school.name == "Example University"
    assert Team.objects.get(team_number=3).is_active is False


def test_success_message_names_the_team_and_the_packet_step(client, event, teams):
    response = _add(client)

    shown = [str(m) for m in response.wsgi_request._messages]
    assert len(shown) == 1
    assert "Team 1" in shown[0]
    assert "Send it the packet" in shown[0]
    assert "password" in shown[0]


def test_refused_with_no_open_event(client, teams):
    response = _add(client)

    assert response.status_code == 200
    assert "Add schools needs the open event" in response.content.decode()
    assert not SchoolInfo.objects.exists()


def test_refused_when_the_school_is_already_on_the_list(client, event, teams):
    _add(client)

    response = _add(client, name="  example   UNIVERSITY ")

    assert response.status_code == 200
    assert "is already on the list" in response.content.decode()
    assert SchoolInfo.objects.count() == 1


def test_refused_when_no_team_is_free(client, event, teams):
    for team, name in zip(teams, ("A College", "B College", "C College"), strict=True):
        set_team_school(team, name, contact_email="a@example.edu", updated_by="x")

    response = _add(client)

    assert response.status_code == 200
    assert "No team is free" in response.content.decode()
    assert SchoolInfo.objects.count() == 3


@pytest.mark.parametrize(
    "user_fixture",
    ["blue_team_user", "red_team_user", "white_team_user", "orange_team_user", "ticketing_support_user"],
)
def test_other_roles_get_403(user_fixture, request, event, teams):
    other = Client()
    other.force_login(request.getfixturevalue(user_fixture))

    assert other.get(reverse("school_info_add")).status_code == 403
    assert _add(other).status_code == 403
    assert not SchoolInfo.objects.exists()


def test_the_form_keeps_typed_values_on_an_error(client, event, teams):
    response = _add(client, contact_email="not-an-email", secondary_email="b@example.edu", notes="keep me")

    page = response.content.decode()
    assert response.status_code == 200
    assert 'value="Example University"' in page
    assert 'value="b@example.edu"' in page
    assert "keep me" in page
    assert not SchoolInfo.objects.exists()


def test_the_button_shows_only_while_an_event_is_open(client, teams):
    assert reverse("school_info_add") not in client.get(reverse("school_info")).content.decode()

    season = Season.objects.create(year=2026, name="2026", is_active=True)
    Event.objects.create(season=season, name="Inv", event_type="invitational", date=date(2026, 10, 3), is_active=True)

    assert reverse("school_info_add") in client.get(reverse("school_info")).content.decode()
