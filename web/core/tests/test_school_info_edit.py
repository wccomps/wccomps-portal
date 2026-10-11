"""Editing a team's school info by hand assigns the team to the active event, as the CSV import does."""

from datetime import date

import pytest
from django.test import Client
from django.urls import reverse
from registration.models import Event, EventTeamAssignment, School, Season, TeamRegistration

from team.models import SchoolInfo, Team

pytestmark = pytest.mark.django_db


@pytest.fixture
def client(gold_team_user):
    client = Client()
    client.force_login(gold_team_user)
    return client


@pytest.fixture
def event():
    season = Season.objects.create(name="2026", year=2026, is_active=True)
    return Event.objects.create(
        season=season, name="Invitational", event_type="invitational", date=date(2026, 10, 3), is_active=True
    )


def _edit(client, team_number, school_name):
    return client.post(
        reverse("school_info_edit", kwargs={"team_number": team_number}),
        {"school_name": school_name, "contact_email": "coach@example.edu"},
    )


def test_new_school_info_assigns_the_team_to_the_active_event(client, event):
    team = Team.objects.create(team_number=4, team_name="Team 04")

    _edit(client, 4, "Example University")

    assignment = EventTeamAssignment.objects.get(event=event)
    assert assignment.team == team
    assert assignment.school.name == "Example University"


def test_renaming_an_assigned_teams_school_moves_its_assignment(client, event):
    team = Team.objects.create(team_number=4, team_name="Team 04")
    registration = TeamRegistration.objects.create(school_name="Example University", status="paid")
    EventTeamAssignment.objects.create(
        event=event, team=team, school=School.for_name("Example University"), registration=registration
    )

    _edit(client, 4, "Other College")

    assert EventTeamAssignment.objects.get(event=event).school.name == "Other College"
    assert SchoolInfo.objects.get(team=team).school_name == "Other College"


def test_spelling_change_keeps_the_school_and_updates_its_name(client, event):
    team = Team.objects.create(team_number=4, team_name="Team 04")
    school = School.for_name("Example University")
    EventTeamAssignment.objects.create(event=event, team=team, school=school)

    _edit(client, 4, "example  university")

    assignment = EventTeamAssignment.objects.get(event=event)
    assert assignment.school_id == school.pk
    assert assignment.school.name == "example university"


def test_renaming_to_a_school_another_team_holds_is_refused(client, event):
    other = Team.objects.create(team_number=2, team_name="Team 02")
    team = Team.objects.create(team_number=4, team_name="Team 04")
    EventTeamAssignment.objects.create(event=event, team=other, school=School.for_name("Other College"))
    mine = EventTeamAssignment.objects.create(event=event, team=team, school=School.for_name("Example University"))

    response = _edit(client, 4, "Other College")

    assert b"already has a team" in response.content
    mine.refresh_from_db()
    assert mine.school.name == "Example University"


def test_school_with_a_team_in_the_event_is_refused(client, event):
    other = Team.objects.create(team_number=2, team_name="Team 02")
    registration = TeamRegistration.objects.create(school_name="Example University", status="paid")
    EventTeamAssignment.objects.create(
        event=event, team=other, school=School.for_name("Example University"), registration=registration
    )
    Team.objects.create(team_number=4, team_name="Team 04")

    response = _edit(client, 4, "Example University")

    assert b"already has a team" in response.content
    assert not SchoolInfo.objects.filter(team__team_number=4).exists()
    assert EventTeamAssignment.objects.count() == 1


def test_no_active_event_saves_school_info_only(client):
    Team.objects.create(team_number=4, team_name="Team 04")

    _edit(client, 4, "Example University")

    assert SchoolInfo.objects.filter(team__team_number=4).exists()
    assert not EventTeamAssignment.objects.exists()


def test_school_list_shows_the_secondary_email(client):
    team = Team.objects.create(team_number=9, team_name="Team 09")
    SchoolInfo.objects.create(
        team=team, school_name="Example U", contact_email="captain@example.edu", secondary_email="coach@example.edu"
    )

    response = client.get(reverse("school_info"))

    assert b"coach@example.edu" in response.content
