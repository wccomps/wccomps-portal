"""The competition page's event panel: which step of running an event is done and which is current, read from the
stage and a few cheap counts."""

from datetime import date
from decimal import Decimal

import pytest
from django.urls import reverse
from packets.models import Packet, PacketDistribution
from scoring.models import ServiceScore

from registration.event_steps import STEPS, EventFacts, StepState, event_facts, event_panel
from registration.models import Event, Season
from registration.services import set_team_school
from registration.stage import EventStage, Stage, event_stage
from team.models import Team

IMPORT, PACKETS, READINESS, START, STOP, FINISH, CLOSE_OUT = (
    "import",
    "packets",
    "readiness",
    "start",
    "stop",
    "finish",
    "close_out",
)


def _facts(stage, *, running=False, packet_sent=False):
    event = (
        Event(name="Invitational", season=Season(name="2026", year=2026))
        if stage in (Stage.READY, Stage.PLAYED)
        else None
    )
    return EventFacts(
        stage=EventStage(stage=stage, event=event, running=running, live_data=False, closed_out_before=False),
        schools=3,
        packet_sent=packet_sent,
        draft_packets=0,
        teams_without_packet=0,
    )


def test_the_steps_run_in_the_order_of_an_event():
    assert [step.key for step in STEPS] == [IMPORT, PACKETS, READINESS, START, STOP, FINISH, CLOSE_OUT]


@pytest.mark.parametrize(
    ("stage", "running", "packet_sent", "done", "current", "action"),
    [
        (Stage.IDLE, False, False, [], [IMPORT], "school_info_import"),
        (Stage.READY, False, False, [IMPORT], [PACKETS, READINESS, START], "packets_list"),
        (Stage.READY, False, True, [IMPORT, PACKETS], [READINESS, START], "admin_competition#readiness"),
        (Stage.READY, True, True, [IMPORT, PACKETS, READINESS, START], [STOP], "admin_competition#controls"),
        (Stage.PLAYED, True, True, [IMPORT, PACKETS, READINESS, START], [STOP], "admin_competition#controls"),
        (
            Stage.PLAYED,
            False,
            True,
            [IMPORT, PACKETS, READINESS, START, STOP],
            [FINISH, CLOSE_OUT],
            "scoring:email_scorecards",
        ),
        # An event can run without packets; the step stays not done
        (Stage.PLAYED, False, False, [IMPORT, READINESS, START, STOP], [FINISH, CLOSE_OUT], "scoring:email_scorecards"),
        (Stage.UNRECORDED, False, False, [READINESS, START, STOP], [CLOSE_OUT], "admin_competition#close-out"),
        (Stage.UNRECORDED, True, False, [READINESS, START], [STOP], "admin_competition#controls"),
        (Stage.LEFTOVERS, False, False, [], [], "admin_competition_danger"),
        (Stage.LEFTOVERS, True, False, [READINESS, START], [STOP], "admin_competition#controls"),
        (Stage.IDLE, True, False, [READINESS, START], [STOP], "admin_competition#controls"),
    ],
)
def test_which_steps_are_done_and_current(stage, running, packet_sent, done, current, action):
    panel = event_panel(_facts(stage, running=running, packet_sent=packet_sent))

    assert [row.step.key for row in panel.steps if row.state == StepState.DONE] == done
    assert [row.step.key for row in panel.steps if row.state == StepState.CURRENT] == current
    assert {row.state for row in panel.steps} <= set(StepState)
    url_name, _, anchor = action.partition("#")
    assert panel.action.url == reverse(url_name) + (f"#{anchor}" if anchor else "")


def test_the_panel_says_the_stage_and_its_next_step():
    facts = _facts(Stage.PLAYED)
    panel = event_panel(facts)

    assert (panel.status, panel.next_step) == (facts.stage.status, facts.stage.next_step)
    assert panel.title == "Invitational, season 2026"
    assert event_panel(_facts(Stage.IDLE)).title == "No event open"


def test_only_the_current_finish_step_shows_the_close_out_checks():
    played = event_panel(_facts(Stage.PLAYED))
    ready = event_panel(_facts(Stage.READY))

    assert [row.step.key for row in played.steps if row.close_out_checks] == [FINISH]
    assert not [row for row in ready.steps if row.close_out_checks]


def test_every_step_has_a_label_a_help_line_and_a_link():
    for step in STEPS:
        assert step.label and step.help and step.link.label
        assert step.link.url


@pytest.mark.django_db
def test_the_facts_count_schools_drafts_and_late_teams(current_event):
    teams = [Team.objects.create(team_number=n, team_name=f"Team {n:02d}", is_active=True) for n in (1, 2, 3)]
    for team in teams:
        set_team_school(team, f"School {team.team_number}", contact_email="c@example.edu")
    # Active but not on the list: a send doesn't reach it
    Team.objects.create(team_number=4, team_name="Team 04", is_active=True)
    Packet.objects.create(title="Draft", file_data=b"x", filename="d.pdf", mime_type="application/pdf", file_size=1)

    before = event_facts(event_stage())
    assert (before.schools, before.packet_sent, before.draft_packets, before.teams_without_packet) == (3, False, 1, 0)

    sent = Packet.objects.create(
        title="Sent",
        file_data=b"x",
        filename="s.pdf",
        mime_type="application/pdf",
        file_size=1,
        status="completed",
        event=current_event,
    )
    PacketDistribution.objects.create(packet=sent, team=teams[0])
    PacketDistribution.objects.create(packet=sent, team=teams[1])

    after = event_facts(event_stage())
    assert (after.packet_sent, after.draft_packets, after.teams_without_packet) == (True, 1, 1)
    packets = next(row for row in event_panel(after).steps if row.step.key == PACKETS)
    assert "1 draft packet is not sent." in packets.detail
    assert "1 team on the list has no copy" in packets.detail


@pytest.mark.django_db
def test_a_packet_sent_for_an_earlier_event_does_not_count(current_event):
    earlier = Event.objects.create(
        season=current_event.season, name="Earlier", event_type="qualifier", date=date(2026, 9, 1)
    )
    Packet.objects.create(
        title="Old",
        file_data=b"x",
        filename="o.pdf",
        mime_type="application/pdf",
        file_size=1,
        status="completed",
        event=earlier,
    )
    team = Team.objects.create(team_number=1, team_name="Team 01", is_active=True)
    set_team_school(team, "School 1", contact_email="c@example.edu")
    ServiceScore.objects.create(team=team, service_points=Decimal(1))

    facts = event_facts(event_stage())
    assert (facts.stage.stage, facts.packet_sent, facts.teams_without_packet) == (Stage.PLAYED, False, 0)
