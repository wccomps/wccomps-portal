"""Finalize freezes every school's scorecard; Clear only runs while that snapshot still matches."""

import threading
from datetime import date
from decimal import Decimal

import pytest
from django.db import connection
from packets.models import Packet
from scoring.models import ServiceScore

from core.models import AuditLog
from registration.models import Event, EventArchive, EventResult, School, Season
from registration.services import (
    FinalizeError,
    SchoolListRow,
    clear_event,
    finalize_event,
    import_problem,
    import_school_list,
    open_event_for_current_schools,
    set_team_school,
    snapshot_matches_live,
)
from team.models import SchoolInfo, Team

pytestmark = pytest.mark.django_db


@pytest.fixture
def event():
    season = Season.objects.create(name="2026", year=2026, is_active=True)
    event = Event.objects.create(
        season=season, name="Inv", event_type="invitational", date=date(2026, 10, 3), is_active=True
    )
    for number, points in ((1, 300), (2, 200)):
        team = Team.objects.create(team_number=number, team_name=f"Team {number:02d}", is_active=True)
        ServiceScore.objects.create(team=team, service_points=Decimal(points))
        set_team_school(team, f"School {number}", contact_email=f"c{number}@example.edu")
    return event


def test_finalize_writes_one_result_per_school_and_the_archive(event):
    assert finalize_event(event, "gold") == 2

    results = list(EventResult.objects.filter(event=event))
    assert [(r.school.name, r.team_number, r.rank) for r in results] == [("School 1", 1, 1), ("School 2", 2, 2)]
    assert results[0].scorecard["team"] == {"team_number": 1}
    assert results[0].total_score == Decimal(300)
    assert EventArchive.objects.get(event=event).data
    event.refresh_from_db()
    assert event.is_finalized
    assert AuditLog.objects.filter(action="event_finalized", target_id=event.pk).exists()


def test_finalize_again_replaces_the_snapshot(event):
    finalize_event(event, "gold")
    ServiceScore.objects.filter(team__team_number=2).update(service_points=Decimal(400))

    finalize_event(event, "gold")

    assert EventResult.objects.get(event=event, rank=1).school.name == "School 2"
    assert EventResult.objects.filter(event=event).count() == 2


def test_results_follow_the_school_list(event):
    set_team_school(Team.objects.get(team_number=2), "Example University", contact_email="c@example.edu")

    finalize_event(event, "gold")

    assert EventResult.objects.get(event=event, team_number=2).school.name == "Example University"


def test_a_closed_event_cannot_be_finalized(event):
    Event.objects.filter(pk=event.pk).update(is_active=False)

    with pytest.raises(FinalizeError, match="not open"):
        finalize_event(event, "gold")
    assert not EventResult.objects.exists()


def test_the_snapshot_matches_live_right_after_finalizing(event):
    finalize_event(event, "gold")

    assert snapshot_matches_live(event)


def test_clear_refuses_after_a_change_since_finalizing(event):
    finalize_event(event, "gold")
    ServiceScore.objects.filter(team__team_number=2).update(service_points=Decimal(400))

    assert not snapshot_matches_live(event)
    with pytest.raises(FinalizeError, match="finalize again"):
        clear_event(event, "gold")
    assert ServiceScore.objects.count() == 2
    event.refresh_from_db()
    assert event.is_active


def test_clear_refuses_an_unfinalized_event(event):
    with pytest.raises(FinalizeError, match="Finalize this event first"):
        clear_event(event, "gold")


def test_clear_refuses_when_finalized_in_another_session(event):
    """The flag is read under the lock, not from the caller's copy of the event."""
    stale = Event.objects.get(pk=event.pk)
    finalize_event(event, "gold")
    Event.objects.filter(pk=event.pk).update(is_finalized=False)
    stale.is_finalized = True

    with pytest.raises(FinalizeError, match="Finalize this event first"):
        clear_event(stale, "gold")


def test_clear_deletes_live_data_keeps_results_and_the_school_list_and_closes_the_event(event):
    finalize_event(event, "gold")

    counts = clear_event(event, "gold")

    assert counts["ServiceScore"] == 2
    assert not ServiceScore.objects.exists()
    assert EventResult.objects.filter(event=event).count() == 2
    assert EventArchive.objects.get(event=event).data
    assert SchoolInfo.objects.count() == 2
    assert event.team_assignments.count() == 2
    event.refresh_from_db()
    assert not event.is_active
    assert event.is_finalized
    assert Event.objects.open() is None
    log = AuditLog.objects.get(action="event_cleared")
    assert log.details["deleted_counts"]["ServiceScore"] == 2


def test_a_cleared_event_cannot_be_cleared_again(event):
    finalize_event(event, "gold")
    clear_event(event, "gold")

    with pytest.raises(FinalizeError, match="not open"):
        clear_event(event, "gold")


def test_clear_records_the_ticket_counter_reset_apart_from_the_deletions(event):
    Team.objects.filter(team_number=1).update(ticket_counter=85)
    finalize_event(event, "gold")

    clear_event(event, "gold")

    details = AuditLog.objects.get(action="event_cleared").details
    assert details["ticket_counters_reset"] == 1
    assert "TeamTicketCounter" not in details["deleted_counts"]
    assert details["total_deleted"] == 2


def test_after_clearing_a_replace_import_opens_the_next_event(event):
    finalize_event(event, "gold")
    blocked = Event(
        season=event.season, name="Qual", event_type="qualifier", date=date(2026, 11, 7), registration_open=False
    )
    assert "still open" in import_problem(blocked)
    clear_event(event, "gold")
    new = Event(
        season=event.season, name="Qual", event_type="qualifier", date=date(2026, 11, 7), registration_open=False
    )
    rows = [SchoolListRow(team_number=2, school_name="Example University", contact_email="c@example.edu")]

    assert import_problem(new) == ""
    import_school_list(new, rows, "gold")

    assert Event.objects.open() == new
    assert list(SchoolInfo.objects.values_list("team__team_number", "school__name")) == [(2, "Example University")]
    assert list(new.team_assignments.values_list("school__name", flat=True)) == ["Example University"]
    assert EventResult.objects.filter(event=event).count() == 2
    assert event.team_assignments.count() == 2


def _sent_packet(**fields):
    return Packet.objects.create(
        title="Rules",
        file_data=b"x",
        filename="r.pdf",
        mime_type="application/pdf",
        file_size=1,
        uploaded_by="gold",
        **fields,
    )


def test_transition_builds_the_event_from_the_current_school_list():
    season = Season.objects.create(name="2026", year=2026, is_active=True)
    first = Team.objects.create(team_number=5, team_name="Team 05", is_active=True)
    second = Team.objects.create(team_number=6, team_name="Team 06", is_active=True)
    set_team_school(first, "Example University", contact_email="c@example.edu")
    set_team_school(second, "School Two", contact_email="c2@example.edu")
    sent = _sent_packet(status="completed")
    sending = _sent_packet(status="distributing")
    draft = _sent_packet(status="draft")
    earlier = Event.objects.create(season=season, name="Earlier", event_type="qualifier", date=date(2026, 9, 1))
    stamped = _sent_packet(status="completed", event=earlier)

    event = open_event_for_current_schools("Invitational 1", date(2026, 10, 3), "invitational")

    assert event.is_active
    assert event.season == season
    assert (event.name, event.date, event.event_type) == ("Invitational 1", date(2026, 10, 3), "invitational")
    assert not event.registration_open
    assert sorted(event.team_assignments.values_list("team__team_number", "school__name")) == [
        (5, "Example University"),
        (6, "School Two"),
    ]
    assert event.team_assignments.get(team=first).school == School.objects.get(name="Example University")
    for packet, owner in ((sent, event), (sending, event), (draft, None), (stamped, earlier)):
        packet.refresh_from_db()
        assert packet.event == owner
    # The school list itself is untouched
    assert SchoolInfo.objects.get(team=first).school_name == "Example University"


def test_transition_refuses_while_an_event_is_open(event):
    with pytest.raises(FinalizeError, match="already open"):
        open_event_for_current_schools("Invitational 2", date(2026, 11, 3), "invitational")
    assert Event.objects.count() == 1


def test_transition_refuses_without_an_active_season():
    Season.objects.create(name="2025", year=2025, is_active=False)

    with pytest.raises(FinalizeError, match="active season"):
        open_event_for_current_schools("Invitational 1", date(2026, 10, 3), "invitational")
    assert not Event.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_two_finalizes_at_once_both_succeed(event):
    errors: list[Exception] = []

    def run():
        try:
            finalize_event(Event.objects.get(pk=event.pk), "gold")
        except Exception as e:  # noqa: BLE001
            errors.append(e)
        finally:
            connection.close()

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert EventResult.objects.filter(event=event).count() == 2
