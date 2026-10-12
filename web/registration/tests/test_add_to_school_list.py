"""Adding schools to the open event: new schools get free teams, schools already on the list keep theirs."""

from datetime import date

import pytest

from registration.models import Event, EventTeamAssignment, School, Season
from registration.services import (
    ImportBlockedError,
    SchoolListRow,
    add_to_school_list,
    free_teams,
    set_team_school,
)
from team.models import SchoolInfo, Team, default_team_name

pytestmark = pytest.mark.django_db


@pytest.fixture
def season():
    return Season.objects.create(name="2026", year=2026, is_active=True)


@pytest.fixture
def event(season):
    return Event.objects.create(
        season=season, name="Invitational", event_type="invitational", date=date(2026, 10, 3), is_active=True
    )


@pytest.fixture
def teams():
    return [Team.objects.create(team_number=n, team_name=f"Team {n:02d}", is_active=False) for n in range(1, 5)]


def _school_on(team, name):
    set_team_school(team, name, contact_email="c@example.edu", updated_by="gold")


def _row(number, name):
    return SchoolListRow(team_number=number, school_name=name, contact_email="new@example.edu")


def _holders():
    return dict(SchoolInfo.objects.values_list("team__team_number", "school__name"))


def test_new_schools_get_their_teams_and_existing_schools_keep_theirs(event, teams):
    _school_on(teams[0], "School One")
    Team.objects.filter(team_number=3).update(team_name="Old Name")

    counts = add_to_school_list(event, [_row(3, "School Two")], "gold")

    assert counts == {"created": 1, "activated": 1}
    assert _holders() == {1: "School One", 3: "School Two"}
    assert dict(event.team_assignments.values_list("team__team_number", "school__name")) == _holders()
    team_3 = Team.objects.get(team_number=3)
    assert team_3.is_active
    assert team_3.team_name == default_team_name(3)
    assert SchoolInfo.objects.get(team=team_3).updated_by == "gold"


def test_adding_never_touches_existing_teams_names_or_assignments(event, teams):
    _school_on(teams[0], "School One")
    Team.objects.filter(team_number=1).update(team_name="School One Team")

    add_to_school_list(event, [_row(2, "School Two")], "gold")

    assert Team.objects.get(team_number=1).team_name == "School One Team"
    assert EventTeamAssignment.objects.get(event=event, team=teams[0]).school.name == "School One"


def test_adding_is_allowed_after_a_packet_went_out_and_teams_linked_discord(event, teams):
    from django.contrib.auth.models import User
    from packets.models import Packet

    from team.models import DiscordLink

    _school_on(teams[0], "School One")
    Packet.objects.create(
        event=event,
        title="Packet",
        file_data=b"x",
        filename="p.pdf",
        mime_type="application/pdf",
        file_size=1,
        status="completed",
        uploaded_by="gold",
    )
    user = User.objects.create(username="member")
    DiscordLink.objects.create(discord_id=1, discord_username="member", user=user, team=teams[0], is_active=True)

    add_to_school_list(event, [_row(2, "School Two")], "gold")

    assert _holders() == {1: "School One", 2: "School Two"}


def test_adding_needs_the_open_event(season, event, teams):
    other = Event.objects.create(season=season, name="Next", event_type="invitational", date=date(2026, 11, 7))

    with pytest.raises(ImportBlockedError, match="Add schools needs the open event"):
        add_to_school_list(other, [_row(2, "School Two")], "gold")

    event.is_active = False
    event.save()
    with pytest.raises(ImportBlockedError, match="Add schools needs the open event"):
        add_to_school_list(event, [_row(2, "School Two")], "gold")
    assert not SchoolInfo.objects.exists()


def test_a_team_given_a_school_since_the_preview_refuses_the_whole_add(event, teams):
    _school_on(teams[1], "School One")

    with pytest.raises(ImportBlockedError, match="Team 2 was given a school since the preview"):
        add_to_school_list(event, [_row(3, "School Two"), _row(2, "School Three")], "gold")

    assert _holders() == {2: "School One"}
    assert not Team.objects.get(team_number=3).is_active


def test_a_team_held_at_the_open_event_alone_is_not_free(event, teams):
    EventTeamAssignment.objects.create(event=event, team=teams[1], school=School.for_name("School One"))

    with pytest.raises(ImportBlockedError, match="Team 2 was given a school since the preview"):
        add_to_school_list(event, [_row(2, "School Two")], "gold")

    assert EventTeamAssignment.objects.get(event=event, team=teams[1]).school.name == "School One"
    assert 2 not in set(free_teams(event).values_list("team_number", flat=True))


def test_a_school_put_on_the_list_since_the_preview_refuses_the_whole_add(event, teams):
    _school_on(teams[0], "School One")

    with pytest.raises(ImportBlockedError, match="school one is already on the list"):
        add_to_school_list(event, [_row(2, "School Two"), _row(3, "school one")], "gold")

    assert _holders() == {1: "School One"}


def test_a_row_for_a_team_that_no_longer_exists_refuses_the_add(event, teams):
    with pytest.raises(ImportBlockedError, match="These teams no longer exist: 9"):
        add_to_school_list(event, [_row(9, "School Two")], "gold")


def test_free_teams_are_those_no_school_holds(event, teams):
    _school_on(teams[0], "School One")

    assert list(free_teams(event).order_by("team_number").values_list("team_number", flat=True)) == [2, 3, 4]
