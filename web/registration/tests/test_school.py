"""A school is one record across events, matched by name ignoring case and spacing."""

from datetime import date

import pytest
from django.db import IntegrityError
from django.test import Client
from django.urls import reverse

from registration.models import Event, EventTeamAssignment, School, Season, TeamRegistration, normalize_school_name
from registration.services import assign_school_to_event
from team.models import Team

pytestmark = pytest.mark.django_db


@pytest.fixture
def event():
    season = Season.objects.create(name="2026", year=2026, is_active=True)
    return Event.objects.create(
        season=season, name="Invitational", event_type="invitational", date=date(2026, 10, 3), is_active=True
    )


@pytest.mark.parametrize(
    "name", ["Example University", "example university", "  Example   University ", "EXAMPLE\tUNIVERSITY"]
)
def test_normalize_ignores_case_and_spacing(name):
    assert normalize_school_name(name) == "example university"


def test_for_name_matches_an_existing_school_and_keeps_the_latest_spelling():
    first = School.for_name("Example university")

    again = School.for_name("  Example   University ")

    assert again.pk == first.pk
    assert School.objects.get().name == "Example University"


def test_assignment_links_the_school_and_a_registration_when_given(event):
    team = Team.objects.create(team_number=4, team_name="Team 04")
    registration = TeamRegistration.objects.create(school_name="Example University", status="paid")

    assignment = assign_school_to_event(event, team, "Example University", registration)

    assert assignment.school.name == "Example University"
    registration.refresh_from_db()
    assert registration.school == assignment.school


def test_one_team_per_school_per_event(event):
    team1 = Team.objects.create(team_number=1, team_name="Team 01")
    team2 = Team.objects.create(team_number=2, team_name="Team 02")
    assign_school_to_event(event, team1, "Example University")

    with pytest.raises(IntegrityError):
        assign_school_to_event(event, team2, "example university")


def test_unassigning_an_imported_school_without_a_registration(gold_team_user, event):
    team = Team.objects.create(team_number=4, team_name="Team 04")
    assignment = assign_school_to_event(event, team, "Example University")
    client = Client()
    client.force_login(gold_team_user)

    response = client.post(reverse("registration_unassign_team", args=[assignment.pk]))

    assert response.status_code == 302
    assert not EventTeamAssignment.objects.exists()


def _enrolled(event, school_name):
    from registration.models import RegistrationEventEnrollment

    registration = TeamRegistration.objects.create(school_name=school_name, status="paid")
    RegistrationEventEnrollment.objects.create(registration=registration, event=event)
    return registration


def test_event_detail_shows_an_imported_assignment_for_an_enrolled_school(gold_team_user, event):
    imported = assign_school_to_event(
        event, Team.objects.create(team_number=4, team_name="Team 04"), "Example University"
    )
    _enrolled(event, "example university")
    client = Client()
    client.force_login(gold_team_user)

    response = client.get(reverse("registration_event_detail", args=[event.pk]))

    [item] = response.context["enrollment_data"]
    assert item["assignment"] == imported
    assert item["can_assign"] is False
    assert b"Team 04" in response.content


def test_assign_teams_skips_a_school_that_already_has_a_team(gold_team_user, event):
    assign_school_to_event(event, Team.objects.create(team_number=4, team_name="Team 04"), "Example University")
    Team.objects.create(team_number=5, team_name="Team 05")
    _enrolled(event, "example university")
    client = Client()
    client.force_login(gold_team_user)

    response = client.post(reverse("registration_assign_teams", args=[event.pk]))

    assert response.status_code == 302
    assert EventTeamAssignment.objects.filter(event=event).count() == 1


def test_assign_teams_gives_one_team_to_registrations_of_the_same_school(gold_team_user, event):
    Team.objects.create(team_number=4, team_name="Team 04")
    Team.objects.create(team_number=5, team_name="Team 05")
    _enrolled(event, "Example University")
    _enrolled(event, "example  university")
    client = Client()
    client.force_login(gold_team_user)

    response = client.post(reverse("registration_assign_teams", args=[event.pk]), follow=True)

    assert EventTeamAssignment.objects.filter(event=event).count() == 1
    content = response.content.decode()
    assert "Assigned 1 team" in content
    assert "already has a team" in content
