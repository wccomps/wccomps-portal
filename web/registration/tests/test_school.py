"""A school is one record across events, matched by name ignoring case and spacing."""

from datetime import date

import pytest
from django.db import IntegrityError
from django.test import Client
from django.urls import reverse

from registration.models import (
    Event,
    EventTeamAssignment,
    RegistrationContact,
    RegistrationEventEnrollment,
    School,
    Season,
    TeamRegistration,
    normalize_school_name,
)
from registration.services import set_team_school
from team.models import SchoolInfo, Team

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


def test_one_team_per_school_per_event(event):
    team1 = Team.objects.create(team_number=1, team_name="Team 01")
    team2 = Team.objects.create(team_number=2, team_name="Team 02")
    EventTeamAssignment.objects.create(event=event, team=team1, school=School.for_name("Example University"))

    with pytest.raises(IntegrityError):
        EventTeamAssignment.objects.create(event=event, team=team2, school=School.for_name("example university"))


def test_unassigning_an_imported_school_without_a_registration(gold_team_user, event):
    team = Team.objects.create(team_number=4, team_name="Team 04")
    set_team_school(team, "Example University", contact_email="c@example.edu")
    client = Client()
    client.force_login(gold_team_user)

    response = client.post(reverse("registration_unassign_team", args=[EventTeamAssignment.objects.get().pk]))

    assert response.status_code == 302
    assert not EventTeamAssignment.objects.exists()
    assert not SchoolInfo.objects.exists()


def _enrolled(event, school_name, *contacts):
    """A paid registration enrolled in the event, with (role, email) contacts; a captain by default."""
    registration = TeamRegistration.objects.create(school_name=school_name, status="paid")
    RegistrationEventEnrollment.objects.create(registration=registration, event=event)
    for role, email in contacts or [("captain", "captain@example.edu")]:
        RegistrationContact.objects.create(registration=registration, role=role, name=role, email=email)
    return registration


def test_event_detail_shows_an_imported_assignment_for_an_enrolled_school(gold_team_user, event):
    set_team_school(
        Team.objects.create(team_number=4, team_name="Team 04"), "Example University", contact_email="c@example.edu"
    )
    imported = EventTeamAssignment.objects.get()
    _enrolled(event, "example university")
    client = Client()
    client.force_login(gold_team_user)

    response = client.get(reverse("registration_event_detail", args=[event.pk]))

    [item] = response.context["enrollment_data"]
    assert item["assignment"] == imported
    assert item["can_assign"] is False
    assert b"Team 04" in response.content


def test_assign_teams_skips_a_school_that_already_has_a_team(gold_team_user, event):
    set_team_school(
        Team.objects.create(team_number=4, team_name="Team 04"), "Example University", contact_email="c@example.edu"
    )
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


def test_assign_teams_adds_each_school_to_the_school_list_with_its_captain(gold_team_user, event):
    Team.objects.create(team_number=4, team_name="Team 04")
    registration = _enrolled(
        event, "Example University", ("coach", "coach@example.edu"), ("captain", "captain@example.edu")
    )
    client = Client()
    client.force_login(gold_team_user)

    client.post(reverse("registration_assign_teams", args=[event.pk]))

    info = SchoolInfo.objects.get()
    assignment = EventTeamAssignment.objects.get(event=event)
    assert info.team == assignment.team
    assert info.school == assignment.school
    assert info.contact_email == "captain@example.edu"
    assert assignment.registration == registration


def test_assign_teams_uses_the_first_contact_without_a_captain(gold_team_user, event):
    Team.objects.create(team_number=4, team_name="Team 04")
    _enrolled(event, "Example University", ("coach", "coach@example.edu"), ("site_judge", "judge@example.edu"))
    client = Client()
    client.force_login(gold_team_user)

    client.post(reverse("registration_assign_teams", args=[event.pk]))

    assert SchoolInfo.objects.get().contact_email == "coach@example.edu"


def test_assign_teams_skips_a_registration_with_no_contact_email(gold_team_user, event):
    Team.objects.create(team_number=4, team_name="Team 04")
    registration = TeamRegistration.objects.create(school_name="Example University", status="paid")
    RegistrationEventEnrollment.objects.create(registration=registration, event=event)
    client = Client()
    client.force_login(gold_team_user)

    response = client.post(reverse("registration_assign_teams", args=[event.pk]), follow=True)

    assert not SchoolInfo.objects.exists()
    assert not EventTeamAssignment.objects.exists()
    assert "Example University: no contact email" in response.content.decode()


def test_assign_teams_skips_a_school_already_in_the_school_list(gold_team_user, event):
    event.is_active = False
    event.save()
    set_team_school(
        Team.objects.create(team_number=4, team_name="Team 04"), "Example University", contact_email="c@example.edu"
    )
    event.open()
    Team.objects.create(team_number=5, team_name="Team 05")
    _enrolled(event, "example university")
    client = Client()
    client.force_login(gold_team_user)

    client.post(reverse("registration_assign_teams", args=[event.pk]))

    assert SchoolInfo.objects.count() == 1
    assert not EventTeamAssignment.objects.exists()


def test_assign_teams_is_refused_for_an_event_that_is_not_open(gold_team_user, event):
    other = Event.objects.create(season=event.season, name="Qualifier", event_type="qualifier", date=date(2026, 1, 3))
    Team.objects.create(team_number=4, team_name="Team 04")
    _enrolled(other, "Example University")
    client = Client()
    client.force_login(gold_team_user)

    response = client.post(reverse("registration_assign_teams", args=[other.pk]), follow=True)

    assert "only be assigned for the open event" in response.content.decode()
    assert not SchoolInfo.objects.exists()
    assert not EventTeamAssignment.objects.exists()


def test_unassigning_an_earlier_events_team_is_refused(gold_team_user, event):
    past = Event.objects.create(season=event.season, name="Qualifier", event_type="qualifier", date=date(2026, 1, 3))
    team = Team.objects.create(team_number=4, team_name="Team 04")
    history = EventTeamAssignment.objects.create(event=past, team=team, school=School.for_name("Old College"))
    set_team_school(team, "Example University", contact_email="c@example.edu")
    client = Client()
    client.force_login(gold_team_user)

    client.post(reverse("registration_unassign_team", args=[history.pk]))

    assert EventTeamAssignment.objects.filter(pk=history.pk).exists()
    assert SchoolInfo.objects.filter(team=team).exists()
