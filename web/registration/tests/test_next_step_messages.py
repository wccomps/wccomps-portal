"""Refusals in the event flow say what to do next and where."""

from decimal import Decimal

import pytest
from packets.services import EARLIER_EVENT_PACKET, NO_EVENT_OPEN, NO_OPEN_EVENT, UNRECORDED_EVENT_PACKET
from scoring.models import ServiceScore

from registration.models import Season
from registration.services import (
    TeamFix,
    cancel_event_problem,
    clear_leftovers_problem,
    close_out_card_check,
    close_out_problem,
    set_team_school,
)
from registration.stage import event_stage
from team.models import Team

pytestmark = pytest.mark.django_db


def close_out_fixes(stage):
    return close_out_card_check(stage.event)[1] if stage.played else []


def test_packet_refusals_about_the_open_event_say_what_to_do():
    assert NO_OPEN_EVENT == "No event is open. Import the next school list to open an event. Then send the packet."
    assert "Upload the packet again" in NO_EVENT_OPEN
    assert EARLIER_EVENT_PACKET.endswith("upload it again as a new packet for the open event.")
    assert UNRECORDED_EVENT_PACKET.endswith("upload it again as a new packet for the open event.")


def test_with_no_event_cancel_and_clear_point_at_the_import():
    stage = event_stage()

    assert cancel_event_problem(stage) == (
        "No event is open, so there is nothing to cancel. Import the next school list to open an event."
    )
    assert clear_leftovers_problem(stage) == (
        "There is no leftover data to clear. Import the next school list to open an event."
    )


def test_a_scored_team_with_no_school_says_where_to_add_it(current_event):
    listed = Team.objects.create(team_number=1, team_name="Team 01", is_active=True)
    set_team_school(listed, "School 1", contact_email="c@example.edu")
    unlisted = Team.objects.create(team_number=2, team_name="Team 02", is_active=True)
    ServiceScore.objects.create(team=unlisted, service_points=Decimal(1))

    assert close_out_problem(event_stage()) == (
        "Team 2 has scores but no school. Enter its school on its School Info page first."
    )


def test_an_event_with_no_schools_says_how_to_list_them(current_event):
    team = Team.objects.create(team_number=1, team_name="Team 01", is_active=True)
    ServiceScore.objects.create(team=team, service_points=Decimal(1))

    assert close_out_problem(event_stage()) == (
        "Invitational (2026) has no schools on its list, so there are no results to keep. Enter its school on its "
        "School Info page first."
    )


def test_an_inactive_scored_team_with_no_school_requires_roster_reconciliation(current_event):
    listed = Team.objects.create(team_number=1, team_name="Team 01", is_active=True)
    set_team_school(listed, "School 1", contact_email="c@example.edu")
    team = Team.objects.create(team_number=2, team_name="Team 02", is_active=False)
    ServiceScore.objects.create(team=team, service_points=Decimal(1))

    stage = event_stage()
    assert close_out_problem(stage) == (
        "Team 2 has scores but no school. For inactive slots, stop and have the event roster owner reconcile "
        "the school mapping against the event roster before retrying. Do not activate a team to bypass this check."
    )
    assert close_out_fixes(stage) == []


def test_a_mapped_inactive_slot_is_not_a_close_out_blocker(current_event):
    team = _scored(1)
    set_team_school(team, "School 1", contact_email="c@example.edu")
    Team.objects.filter(pk=team.pk).update(is_active=False)
    stage = event_stage()
    assert close_out_problem(stage) == "" and close_out_fixes(stage) == []


def _scored(number, *, active=True):
    team = Team.objects.create(team_number=number, team_name=f"Team {number:02d}", is_active=active)
    ServiceScore.objects.create(team=team, service_points=Decimal(1))
    return team


@pytest.mark.parametrize(
    ("unschooled", "named"), [(3, "Team 2, Team 3, Team 4"), (4, "Team 2, Team 3, Team 4 and 1 more")]
)
def test_scored_teams_with_no_school_are_named_up_to_three_and_each_told_to_enter_theirs(
    current_event, unschooled, named
):
    set_team_school(_scored(1), "School 1", contact_email="c@example.edu")
    for number in range(2, 2 + unschooled):
        _scored(number)

    stage = event_stage()
    assert close_out_problem(stage) == (
        f"{named} have scores but no school. Enter each team's school on its School Info page first."
    )
    assert close_out_fixes(stage) == [
        TeamFix(number, activate=False, enter_school=True, remove_school=False) for number in (2, 3, 4)
    ]


def test_with_nothing_played_there_is_no_team_to_fix(current_event):
    """An inactive listed team holds back no Close Out while nothing was played: the page offers it no fix."""
    team = Team.objects.create(team_number=1, team_name="Team 01", is_active=True)
    set_team_school(team, "School 1", contact_email="c@example.edu")
    Team.objects.update(is_active=False)

    assert close_out_fixes(event_stage()) == []


def test_the_school_checks_wait_for_the_close_out_itself(current_event):
    """The competition page asks without them, so it offers Close Out; the close-out's own check refuses."""
    _scored(1)

    stage = event_stage()
    assert close_out_problem(stage, check_schools=False) == ""
    assert "no schools on its list" in close_out_problem(stage)


def test_with_no_event_open_and_no_active_season_close_out_points_at_the_seasons_page():
    _scored(1)

    assert close_out_problem(event_stage()) == (
        "No season is active, and Close Out records these scores at a new event in the active season. Make a season "
        "active on the Seasons page, then close out."
    )


def test_mixed_unmapped_slots_only_link_to_the_active_school_editor(current_event):
    set_team_school(_scored(1), "School 1", contact_email="c@example.edu")
    _scored(2)
    _scored(3, active=False)

    stage = event_stage()
    assert close_out_problem(stage) == (
        "Team 2, Team 3 have scores but no school. For inactive slots, stop and have the event roster owner "
        "reconcile the school mapping against the event roster before retrying. Do not activate a team to bypass "
        "this check. For active slots, enter the school on the School Info page."
    )
    assert close_out_fixes(stage) == [
        TeamFix(2, activate=False, enter_school=True, remove_school=False),
    ]


def test_with_no_event_open_and_an_empty_school_list_the_scores_have_nowhere_to_go():
    Season.objects.create(name="2026", year=2026, is_active=True)
    _scored(1)

    assert close_out_problem(event_stage()) == (
        "The school list is empty, so there are no results to keep. Enter its school on its School Info page first."
    )
