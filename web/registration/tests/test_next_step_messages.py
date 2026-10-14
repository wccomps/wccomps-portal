"""Refusals in the event flow say what to do next and where."""

from decimal import Decimal

import pytest
from packets.services import EARLIER_EVENT_PACKET, NO_EVENT_OPEN, NO_OPEN_EVENT, UNRECORDED_EVENT_PACKET
from scoring.models import ServiceScore

from registration.services import cancel_event_problem, clear_leftovers_problem, close_out_problem
from registration.stage import event_stage
from team.models import Team

pytestmark = pytest.mark.django_db


def test_packet_refusals_about_the_open_event_say_what_to_do():
    assert NO_OPEN_EVENT == (
        "No event is open: import the school list for this event first (Schools, Import from CSV), then send the "
        "packet."
    )
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
    from registration.services import set_team_school

    set_team_school(listed, "School 1", contact_email="c@example.edu")
    unlisted = Team.objects.create(team_number=2, team_name="Team 02", is_active=True)
    ServiceScore.objects.create(team=unlisted, service_points=Decimal(1))

    assert close_out_problem(event_stage()) == (
        "Team 2 has scores but no school: on the Schools page, open the team and enter its school first."
    )


def test_an_event_with_no_schools_says_how_to_list_them(current_event):
    team = Team.objects.create(team_number=1, team_name="Team 01", is_active=True)
    ServiceScore.objects.create(team=team, service_points=Decimal(1))

    assert close_out_problem(event_stage()) == (
        "Invitational (2026) has no schools on its list, so there are no results to keep. On the Schools page, open "
        "each team with scores and enter its school first."
    )
