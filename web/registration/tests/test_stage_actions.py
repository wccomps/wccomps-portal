"""The lifecycle table: for each stage, whether the competition runs and what is done, the one next action."""

from datetime import date
from decimal import Decimal

import pytest
from scoring.models import ServiceScore

from core.models import AuditLog
from registration.models import Event, Season
from registration.stage import ACTIONS, Action, EventStage, Stage, event_stage
from team.models import Team


def _stage(stage, *, running=False, packets_sent=False, scorecards_emailed=False):
    event = Event(name="Inv", season=Season(name="2026", year=2026)) if stage in (Stage.READY, Stage.PLAYED) else None
    return EventStage(
        stage=stage,
        event=event,
        running=running,
        live_data=stage != Stage.IDLE,
        closed_out_before=False,
        packets_sent=packets_sent,
        scorecards_emailed=scorecards_emailed,
    )


@pytest.mark.parametrize(
    ("stage", "facts", "action"),
    [
        (Stage.IDLE, {}, Action.IMPORT),
        (Stage.READY, {}, Action.SEND_PACKETS),
        (Stage.READY, {"packets_sent": True}, Action.START),
        (Stage.READY, {"packets_sent": True, "running": True}, Action.STOP),
        (Stage.PLAYED, {"running": True}, Action.STOP),
        (Stage.PLAYED, {}, Action.FINISH),
        (Stage.PLAYED, {"scorecards_emailed": True}, Action.CLOSE_OUT),
        # Scores no event recorded: scoring and the scorecards still come before the close-out that deletes them
        (Stage.UNRECORDED, {}, Action.FINISH),
        (Stage.UNRECORDED, {"scorecards_emailed": True}, Action.CLOSE_OUT),
        (Stage.LEFTOVERS, {}, Action.CLEAR),
        (Stage.IDLE, {"running": True}, Action.STOP),
    ],
)
def test_each_stage_has_one_next_action(stage, facts, action):
    read = _stage(stage, **facts)

    assert read.next_action == action
    assert read.next_step == ACTIONS[action].instruction.format(event=read.event)


def test_every_action_says_what_to_do_and_names_its_link():
    for action in Action:
        assert ACTIONS[action].instruction.endswith(".") and ACTIONS[action].link_label


@pytest.mark.django_db
def test_the_stage_reads_the_packets_and_scorecard_emails(current_event):
    from packets.models import Packet

    team = Team.objects.create(team_number=1, team_name="Team 01", is_active=True)
    ServiceScore.objects.create(team=team, service_points=Decimal(1))
    assert (event_stage().packets_sent, event_stage().scorecards_emailed) == (False, False)

    Packet.objects.create(
        title="P",
        file_data=b"x",
        filename="p.pdf",
        mime_type="application/pdf",
        file_size=1,
        status="completed",
        event=current_event,
    )
    AuditLog.objects.create(
        action="scorecards_emailed",
        admin_user="gold",
        target_entity="event",
        target_id=current_event.pk,
        details={"event": current_event.pk},
    )
    assert (event_stage().packets_sent, event_stage().scorecards_emailed) == (True, True)


@pytest.mark.django_db
def test_with_no_event_only_sends_since_the_last_clear_count():
    team = Team.objects.create(team_number=1, team_name="Team 01", is_active=True)
    ServiceScore.objects.create(team=team, service_points=Decimal(1))
    old = AuditLog.objects.create(
        action="scorecards_emailed", admin_user="gold", target_entity="event", target_id=0, details={"event": None}
    )
    AuditLog.objects.create(action="event_closed_out", admin_user="gold", target_entity="event", target_id=1)
    AuditLog.objects.filter(pk=old.pk).update(created_at=date(2026, 1, 1))
    assert not event_stage().scorecards_emailed

    AuditLog.objects.create(
        action="scorecards_emailed", admin_user="gold", target_entity="event", target_id=0, details={"event": None}
    )
    assert event_stage().scorecards_emailed
