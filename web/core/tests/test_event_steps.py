"""The Event panel's steps: which is done and which is current, read from the stage and a few cheap counts."""

import itertools
from datetime import date
from decimal import Decimal

import pytest
from django.urls import reverse
from packets.models import Packet, PacketDistribution
from registration.models import Event, Season
from registration.services import set_team_school
from registration.stage import Action, EventStage, Stage, event_stage
from scoring.models import ServiceScore

from core.admin_views.event_panel import (
    PLACES,
    STEPS,
    EventFacts,
    Section,
    StepState,
    event_facts,
    event_panel,
)
from team.models import Team


def _stage(stage, *, running=False, packets_sent=False, scorecards_emailed=False):
    event = (
        Event(name="Invitational", season=Season(name="2026", year=2026))
        if stage in (Stage.READY, Stage.PLAYED)
        else None
    )
    return EventStage(
        stage=stage,
        event=event,
        running=running,
        live_data=stage != Stage.IDLE,
        closed_out_before=False,
        packets_sent=packets_sent,
        scorecards_emailed=scorecards_emailed,
    )


def _panel(stage, **facts):
    return event_panel(EventFacts(_stage(stage, **facts), schools=3, draft_packets=0, teams_without_copy=0))


EVERY_READING = [
    (stage, {"running": running, "packets_sent": sent, "scorecards_emailed": emailed})
    for stage, running, sent, emailed in itertools.product(Stage, (False, True), (False, True), (False, True))
]


def test_the_steps_run_in_the_order_of_an_event():
    assert [step.action for step in STEPS] == [
        Action.IMPORT,
        Action.SEND_PACKETS,
        Action.START,
        Action.STOP,
        Action.FINISH,
        Action.CLOSE_OUT,
    ]


@pytest.mark.parametrize(("stage", "facts"), EVERY_READING)
def test_the_next_step_and_the_current_step_are_the_same_action(stage, facts):
    panel = _panel(stage, **facts)

    current = [row.step.action for row in panel.steps if row.state == StepState.CURRENT]
    stepped = {step.action for step in STEPS}
    assert current == ([panel.stage.next_action] if panel.stage.next_action in stepped else [])
    assert panel.action_url == reverse(PLACES[panel.stage.next_action][0]) + (
        f"#{PLACES[panel.stage.next_action][1]}" if PLACES[panel.stage.next_action][1] else ""
    )


@pytest.mark.parametrize(
    ("stage", "facts", "done"),
    [
        (Stage.IDLE, {}, []),
        (Stage.READY, {}, [Action.IMPORT]),
        (Stage.READY, {"packets_sent": True}, [Action.IMPORT, Action.SEND_PACKETS]),
        (Stage.PLAYED, {"running": True, "packets_sent": True}, [Action.IMPORT, Action.SEND_PACKETS, Action.START]),
        (Stage.PLAYED, {}, [Action.IMPORT, Action.START, Action.STOP]),
        (
            Stage.PLAYED,
            {"packets_sent": True, "scorecards_emailed": True},
            [Action.IMPORT, Action.SEND_PACKETS, Action.START, Action.STOP, Action.FINISH],
        ),
        (Stage.UNRECORDED, {}, [Action.START, Action.STOP]),
        (Stage.LEFTOVERS, {}, []),
    ],
)
def test_which_steps_are_done(stage, facts, done):
    panel = _panel(stage, **facts)

    assert [row.step.action for row in panel.steps if row.state == StepState.DONE] == done


def test_the_panel_links_the_seasons_page():
    assert _panel(Stage.PLAYED).seasons_url == reverse("registration_season_list")


@pytest.mark.parametrize(
    ("stage", "facts", "following"),
    [
        (Stage.READY, {}, [Action.START]),
        (Stage.READY, {"packets_sent": True}, []),
        (Stage.PLAYED, {}, [Action.CLOSE_OUT]),
        (Stage.PLAYED, {"running": True}, []),
        (Stage.PLAYED, {"scorecards_emailed": True}, []),
        (Stage.UNRECORDED, {}, [Action.CLOSE_OUT]),
        (Stage.IDLE, {}, []),
    ],
)
def test_the_stages_steps_after_the_current_one_are_next_and_the_rest_later(stage, facts, following):
    panel = _panel(stage, **facts)

    assert [row.step.action for row in panel.steps if row.state == StepState.NEXT] == following
    assert {row.label for row in panel.steps} <= {"Done", "Now", "Next", "Later"}


@pytest.mark.parametrize(
    ("stage", "facts", "section"),
    [
        (Stage.IDLE, {}, None),
        (Stage.READY, {}, None),
        (Stage.READY, {"packets_sent": True}, Section.READINESS),
        (Stage.PLAYED, {"running": True}, Section.RUNNING),
        (Stage.IDLE, {"running": True}, Section.RUNNING),
        (Stage.PLAYED, {}, Section.CLOSE_OUT),
        (Stage.PLAYED, {"scorecards_emailed": True}, Section.CLOSE_OUT),
        (Stage.UNRECORDED, {}, Section.CLOSE_OUT),
        (Stage.LEFTOVERS, {}, Section.LEFTOVERS),
    ],
)
def test_the_page_shows_the_section_of_the_next_step_only(stage, facts, section):
    assert _panel(stage, **facts).section == section


def test_every_step_has_a_label_and_a_link():
    for step in STEPS:
        assert step.label and step.link_label and step.url


@pytest.mark.django_db
def test_the_facts_count_schools_drafts_and_teams_without_a_delivered_copy(current_event):
    teams = [Team.objects.create(team_number=n, team_name=f"Team {n:02d}", is_active=True) for n in (1, 2, 3)]
    for team in teams:
        set_team_school(team, f"School {team.team_number}", contact_email="c@example.edu")
    # Active but not on the list: a send doesn't reach it
    Team.objects.create(team_number=4, team_name="Team 04", is_active=True)
    Packet.objects.create(title="Draft", file_data=b"x", filename="d.pdf", mime_type="application/pdf", file_size=1)

    before = event_facts(event_stage())
    assert (before.schools, before.stage.packets_sent, before.draft_packets, before.teams_without_copy) == (
        3,
        False,
        1,
        0,
    )

    sent = Packet.objects.create(
        title="Sent",
        file_data=b"x",
        filename="s.pdf",
        mime_type="application/pdf",
        file_size=1,
        status="completed",
        event=current_event,
    )
    PacketDistribution.objects.create(packet=sent, team=teams[0], email_status="sent")
    PacketDistribution.objects.create(packet=sent, team=teams[1], email_status="failed")

    after = event_facts(event_stage())
    assert (after.stage.packets_sent, after.draft_packets, after.teams_without_copy) == (True, 1, 2)
    packets = next(row for row in event_panel(after).steps if row.step.action == Action.SEND_PACKETS)
    assert "1 draft packet is not sent." in packets.detail
    assert "2 teams on the list have no delivered copy" in packets.detail


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
    assert (facts.stage.stage, facts.stage.packets_sent, facts.teams_without_copy) == (Stage.PLAYED, False, 0)
