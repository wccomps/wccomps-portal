"""The school list owns which school holds a team; the open event's assignment follows it."""

from datetime import date

import pytest

from registration.models import (
    Event,
    EventTeamAssignment,
    RegistrationEventEnrollment,
    School,
    Season,
    TeamRegistration,
)
from registration.services import SchoolTakenError, remove_team_school, set_team_school
from team.models import SchoolInfo, Team

pytestmark = pytest.mark.django_db


@pytest.fixture
def season():
    return Season.objects.create(name="2026", year=2026, is_active=True)


@pytest.fixture
def event(season):
    return Event.objects.create(
        season=season, name="Invitational", event_type="invitational", date=date(2026, 10, 3), is_active=True
    )


def _team(number):
    return Team.objects.create(team_number=number, team_name=f"Team {number:02d}")


def test_with_no_open_event_only_the_school_list_changes(season):
    team = _team(4)

    info = set_team_school(team, "Example University", contact_email="c@example.edu", updated_by="gold")

    assert SchoolInfo.objects.get(team=team) == info
    assert info.school.name == "Example University"
    assert info.contact_email == "c@example.edu"
    assert info.updated_by == "gold"
    assert not EventTeamAssignment.objects.exists()


def test_with_an_open_event_its_assignment_follows_the_school_list(event):
    team = _team(4)

    info = set_team_school(team, "Example University", contact_email="c@example.edu")

    assignment = EventTeamAssignment.objects.get(event=event)
    assert assignment.team == team
    assert assignment.school == info.school


def test_changing_the_school_moves_the_assignment_and_leaves_the_old_registration(event):
    team = _team(4)
    registration = TeamRegistration.objects.create(school_name="Example University", status="approved")
    old = set_team_school(team, "Example University", contact_email="c@example.edu", registration=registration).school

    info = set_team_school(team, "Other College", contact_email="c@example.edu", secondary_email="b@example.edu")

    assignment = EventTeamAssignment.objects.get(event=event)
    assert assignment.school == info.school
    assert assignment.registration is None
    registration.refresh_from_db()
    assert registration.school == old
    assert info.secondary_email == "b@example.edu"
    assert SchoolInfo.objects.count() == 1


def test_respelling_the_school_keeps_its_registration(event):
    team = _team(4)
    registration = TeamRegistration.objects.create(school_name="Example University", status="approved")
    set_team_school(team, "Example University", contact_email="c@example.edu", registration=registration)

    set_team_school(team, "example  UNIVERSITY", contact_email="c@example.edu")

    assert EventTeamAssignment.objects.get(event=event).registration == registration


def test_renaming_to_a_new_spelling_renames_the_record_and_leaves_registrations_as_typed(event):
    """The record's own key matches the new spelling, so it takes the name; the registrations of the school still name
    it (by key), so they keep the spelling their school typed."""
    team = _team(4)
    registration = TeamRegistration.objects.create(school_name="Example University", status="approved")
    RegistrationEventEnrollment.objects.create(registration=registration, event=event)
    set_team_school(team, "Example University", contact_email="c@example.edu", registration=registration)

    info = set_team_school(team, "EXAMPLE   university", contact_email="c@example.edu", rename=True)

    assert School.objects.get() == info.school
    assert info.school.name == "EXAMPLE university"
    registration.refresh_from_db()
    assert registration.school_name == "Example University"


@pytest.mark.parametrize("school_name", ["Other College", "Example University"], ids=["changed", "kept"])
def test_an_assignment_with_no_registration_takes_the_one_given_later(event, school_name):
    """The team's assignment, made without a registration (as an import makes it), takes the one given later, and the
    registration takes the school, whether the school changes or stays."""
    team = _team(4)
    set_team_school(team, "Example University", contact_email="c@example.edu")
    registration = TeamRegistration.objects.create(school_name=school_name, status="approved")

    info = set_team_school(team, school_name, contact_email="c@example.edu", registration=registration)

    assert EventTeamAssignment.objects.get(event=event).registration == registration
    registration.refresh_from_db()
    assert registration.school == info.school


def test_a_given_registration_is_linked_to_the_assignment_and_the_school(event):
    registration = TeamRegistration.objects.create(school_name="Example University", status="approved")

    info = set_team_school(_team(4), "Example University", contact_email="c@example.edu", registration=registration)

    assert EventTeamAssignment.objects.get(event=event).registration == registration
    registration.refresh_from_db()
    assert registration.school == info.school


@pytest.mark.parametrize("open_event", [False, True])
def test_two_teams_cannot_hold_one_school(season, open_event):
    if open_event:
        Event.objects.create(
            season=season, name="Invitational", event_type="invitational", date=date(2026, 10, 3), is_active=True
        )
    set_team_school(_team(2), "Example University", contact_email="a@example.edu")
    team = _team(4)

    with pytest.raises(SchoolTakenError, match=r"(?i)example university already has a team: Team 2"):
        set_team_school(team, "example  university", contact_email="c@example.edu")

    assert not SchoolInfo.objects.filter(team=team).exists()
    assert School.objects.get().name == "Example University"
    assert EventTeamAssignment.objects.count() == (1 if open_event else 0)


def test_a_school_held_at_the_open_event_is_refused_even_without_school_info(event):
    EventTeamAssignment.objects.create(event=event, team=_team(2), school=School.for_name("Example University"))

    with pytest.raises(SchoolTakenError, match="Team 2"):
        set_team_school(_team(4), "Example University", contact_email="c@example.edu")


def test_removing_a_teams_school_drops_the_open_events_assignment_and_keeps_history(season, event):
    past = Event.objects.create(season=season, name="Qualifier", event_type="qualifier", date=date(2026, 1, 3))
    team = _team(4)
    EventTeamAssignment.objects.create(event=past, team=team, school=School.for_name("Old College"))
    set_team_school(team, "Example University", contact_email="c@example.edu")

    remove_team_school(team)

    assert not SchoolInfo.objects.exists()
    assert list(EventTeamAssignment.objects.values_list("event", flat=True)) == [past.pk]


def test_expecting_a_free_team_refuses_one_that_has_a_school(event):
    from registration.services import TeamTakenError

    team = _team(4)
    set_team_school(team, "Other College", contact_email="o@example.edu")

    with pytest.raises(TeamTakenError, match="Team 4 already has a school: Other College"):
        set_team_school(team, "Example University", contact_email="c@example.edu", expect_free=True)

    assert SchoolInfo.objects.get(team=team).school_name == "Other College"
    assert EventTeamAssignment.objects.get(event=event).school.name == "Other College"


def test_removing_with_the_school_expected_refuses_after_a_change(event):
    from registration.services import SchoolChangedError

    team = _team(4)
    shown = set_team_school(team, "Example University", contact_email="c@example.edu").school
    set_team_school(team, "Other College", contact_email="o@example.edu")

    with pytest.raises(SchoolChangedError, match="Team 4's school changed since you opened this page; reload."):
        remove_team_school(team, expected_school_id=shown.pk)

    assert SchoolInfo.objects.get(team=team).school_name == "Other College"
    assert EventTeamAssignment.objects.get(event=event).school.name == "Other College"


def test_removing_with_the_school_expected_finds_a_school_held_only_at_the_open_event(event):
    """An assignment made at the event with no school list row (as the event page shows it) is the team's school."""
    team = _team(4)
    school = School.for_name("Example University")
    EventTeamAssignment.objects.create(event=event, team=team, school=school)

    remove_team_school(team, expected_school_id=school.pk)

    assert not EventTeamAssignment.objects.exists()


def test_removing_with_the_school_expected_goes_by_the_school_list_when_the_event_has_no_assignment(season):
    """The school was listed before the event opened, so the event holds no assignment for it yet."""
    team = _team(4)
    school = set_team_school(team, "Example University", contact_email="c@example.edu").school
    Event.objects.create(season=season, name="Invitational", event_type="invitational", date=date(2026, 10, 3)).open()

    remove_team_school(team, expected_school_id=school.pk)

    assert not SchoolInfo.objects.exists()
