"""Adding schools to the open event: new schools get free teams, schools already on the list keep theirs."""

from datetime import date

import pytest

from registration.models import Event, EventTeamAssignment, School, Season
from registration.services import (
    ImportBlockedError,
    SchoolListRow,
    add_to_school_list,
    free_teams,
    held_schools,
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


@pytest.mark.parametrize("leftover", ["discord_link", "ticket", "ticket_counter"])
def test_a_team_still_carrying_per_team_data_is_not_free(event, teams, leftover):
    from django.contrib.auth.models import User

    from team.models import DiscordLink
    from ticketing.models import Ticket

    if leftover == "discord_link":
        user = User.objects.create(username="member")
        DiscordLink.objects.create(discord_id=1, discord_username="member", user=user, team=teams[1], is_active=True)
    elif leftover == "ticket":
        Ticket.objects.create(ticket_number="T002-001", team=teams[1], title="Help")
    else:
        Team.objects.filter(pk=teams[1].pk).update(ticket_counter=1)

    assert 2 not in set(free_teams(event).values_list("team_number", flat=True))
    with pytest.raises(ImportBlockedError, match="Team 2 was given a school since the preview"):
        add_to_school_list(event, [_row(2, "School Two")], "gold")


def test_held_schools_are_those_on_the_list_or_at_the_event(event, teams):
    _school_on(teams[0], "School One")
    EventTeamAssignment.objects.create(event=event, team=teams[1], school=School.for_name("School Two"))

    assert held_schools(["school one", "school two", "school three"], event) == {"school one", "school two"}


def test_handing_over_a_team_with_old_channels_queues_their_removal_ahead_of_a_link(event, teams):
    """The last school's role and category outlived its teardown (the bot was down, or a delete failed): the team is
    still free, and handing it over queues their removal before any setup a new member's link queues."""
    from core.discord_tasks import SetupTeamInfrastructure
    from core.models import DiscordTask

    Team.objects.filter(pk=teams[1].pk).update(discord_role_id=5, discord_category_id=6)
    assert 2 in set(free_teams(event).values_list("team_number", flat=True))

    add_to_school_list(event, [_row(2, "School Two")], "gold")
    DiscordTask.enqueue(SetupTeamInfrastructure(team_number=2))

    queued = DiscordTask.objects.order_by("created_at", "pk").values_list("task_type", "payload__team_number")
    assert list(queued) == [("clear_team_channels", 2), ("setup_team_infrastructure", 2)]
    assert Team.objects.get(pk=teams[1].pk).channels_need_clearing


def test_two_hand_overs_queue_one_removal(event, teams):
    from core.models import DiscordTask
    from registration.services import set_team_school

    Team.objects.filter(pk=teams[1].pk).update(discord_role_id=5, discord_category_id=6)
    set_team_school(teams[1], "School Two", contact_email="a@example.com")
    set_team_school(teams[1], "School Three", contact_email="a@example.com")

    assert list(DiscordTask.objects.values_list("task_type", flat=True)) == ["clear_team_channels"]


def test_the_same_school_keeping_its_team_queues_no_removal(event, teams):
    from core.models import DiscordTask
    from registration.services import set_team_school

    set_team_school(teams[1], "School Two", contact_email="a@example.com")
    Team.objects.filter(pk=teams[1].pk).update(discord_role_id=5, discord_category_id=6)
    set_team_school(teams[1], "School Two", contact_email="b@example.com")

    assert not DiscordTask.objects.exists()
    assert not Team.objects.get(pk=teams[1].pk).channels_need_clearing


def test_clearing_the_school_list_is_refused_while_a_team_has_linked_members(event, teams):
    from django.contrib.auth.models import User

    from registration.services import LINKED_TEAMS_CLEAR, TeamLinkedError, clear_school_list, set_team_school
    from team.models import DiscordLink

    set_team_school(teams[0], "School One", contact_email="a@example.com")
    user = User.objects.create(username="member01")
    DiscordLink.objects.create(discord_id=11, discord_username="m", user=user, team=teams[0], is_active=True)

    with pytest.raises(TeamLinkedError, match="Close Out Event or Clear leftover data unlinks them first"):
        clear_school_list()

    assert str(LINKED_TEAMS_CLEAR).startswith("Teams have linked Discord members")
    assert SchoolInfo.objects.exists()
