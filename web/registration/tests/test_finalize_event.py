"""Finalize freezes every school's scorecard; Clear only runs while that snapshot still matches."""

import io
import threading
import zipfile
from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.contrib.auth.models import User
from django.db import connection
from django.utils import timezone
from packets.models import Packet
from scoring.export import DATASETS
from scoring.models import InjectScore, RedTeamScore, ServiceDetail, ServiceScore
from scoring.scorecard import build_scorecard_context

from core.models import AuditLog, CompetitionConfig
from registration.models import Event, EventArchive, EventResult, School, Season
from registration.services import (
    FinalizeError,
    NewEvent,
    SchoolListRow,
    changed_teams,
    clear_event,
    finalize_open_event,
    import_problem,
    import_school_list,
    set_team_school,
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
    assert finalize_open_event(None, "gold").results == 2

    results = list(EventResult.objects.filter(event=event))
    assert [(r.school.name, r.team_number, r.rank) for r in results] == [("School 1", 1, 1), ("School 2", 2, 2)]
    assert results[0].scorecard["team"] == {"team_number": 1}
    assert results[0].total_score == Decimal(300)
    assert EventArchive.objects.get(event=event).data
    event.refresh_from_db()
    assert event.is_finalized
    assert AuditLog.objects.filter(action="event_finalized", target_id=event.pk).exists()


def test_finalize_again_replaces_the_snapshot(event):
    finalize_open_event(None, "gold")
    ServiceScore.objects.filter(team__team_number=2).update(service_points=Decimal(400))

    finalize_open_event(None, "gold")

    assert EventResult.objects.get(event=event, rank=1).school.name == "School 2"
    assert EventResult.objects.filter(event=event).count() == 2


def test_results_follow_the_school_list(event):
    set_team_school(Team.objects.get(team_number=2), "Example University", contact_email="c@example.edu")

    finalize_open_event(None, "gold")

    assert EventResult.objects.get(event=event, team_number=2).school.name == "Example University"


def test_with_no_event_open_finalize_needs_the_new_event(event):
    Event.objects.filter(pk=event.pk).update(is_active=False)

    with pytest.raises(FinalizeError, match="No event is open"):
        finalize_open_event(None, "gold")
    assert not EventResult.objects.exists()


def test_a_new_event_is_refused_once_an_event_is_open(event):
    """The page offered the new event's fields before an import opened one."""
    with pytest.raises(FinalizeError, match="Reload the page"):
        finalize_open_event(NewEvent("Invitational 2", date(2026, 11, 3), "invitational"), "gold")
    assert Event.objects.count() == 1
    assert not EventResult.objects.exists()


def test_finalize_refuses_while_the_competition_runs(event):
    CompetitionConfig.objects.update_or_create(pk=1, defaults={"applications_enabled": True})

    with pytest.raises(FinalizeError, match="Stop the competition before finalizing"):
        finalize_open_event(None, "gold")
    assert not EventResult.objects.exists()


def test_finalize_refuses_an_inactive_team_holding_a_school(event):
    Team.objects.filter(team_number=2).update(is_active=False)

    with pytest.raises(FinalizeError, match="Reactivate Team 2 or remove its school first"):
        finalize_open_event(None, "gold")
    assert not EventResult.objects.exists()


def test_finalize_refuses_a_scored_team_with_no_school(event):
    unlisted = Team.objects.create(team_number=3, team_name="Team 03", is_active=False)
    InjectScore.objects.create(team=unlisted, inject_id="1", inject_name="Memo", points_awarded=Decimal(5))

    with pytest.raises(FinalizeError, match="Team 3 has scores but no school"):
        finalize_open_event(None, "gold")
    assert not EventResult.objects.exists()


def test_finalize_refuses_a_red_finding_against_a_team_with_no_school(event):
    unlisted = Team.objects.create(team_number=3, team_name="Team 03", is_active=True)
    finding = RedTeamScore.objects.create(
        attack_vector="ssh", points_per_team=Decimal(10), submitted_by=User.objects.create_user("red")
    )
    finding.affected_teams.add(unlisted)

    with pytest.raises(FinalizeError, match="Team 3 has scores but no school"):
        finalize_open_event(None, "gold")


def test_finalize_refuses_an_event_with_no_schools():
    season = Season.objects.create(name="2026", year=2026, is_active=True)
    Event.objects.create(season=season, name="Inv", event_type="invitational", date=date(2026, 10, 3), is_active=True)

    with pytest.raises(FinalizeError, match="no schools"):
        finalize_open_event(None, "gold")
    assert not EventResult.objects.exists()


def test_no_team_changed_right_after_finalizing(event):
    finalize_open_event(None, "gold")

    assert changed_teams(event) == []


def test_no_team_changed_with_averaged_decimals(event):
    """Averages over three teams give Decimals with many places; they must compare equal after storing."""
    third = Team.objects.create(team_number=3, team_name="Team 03", is_active=True)
    set_team_school(third, "School 3", contact_email="c3@example.edu")
    for team, points in zip(Team.objects.order_by("team_number"), ("100.00", "200.00", "200.01"), strict=True):
        ServiceDetail.objects.create(team=team, service_name="web", points=Decimal(points))
    finalize_open_event(None, "gold")

    assert changed_teams(event) == []


def test_no_team_changed_with_an_int_keyed_dict(event):
    """JSON stores dict keys as strings, so a scorecard value keyed by numbers must still compare equal."""

    def with_int_keys(score, standings):
        context = build_scorecard_context(score, standings)
        context["stats"] = {**context["stats"], "by_team": {score.team.team_number: Decimal("0.5")}}
        return context

    with patch("registration.services.build_scorecard_context", with_int_keys):
        finalize_open_event(None, "gold")

        assert changed_teams(event) == []


def test_clear_refuses_after_a_change_since_finalizing(event):
    finalize_open_event(None, "gold")
    ServiceScore.objects.filter(team__team_number=2).update(service_points=Decimal(400))

    # Team 1's scorecard compares it with the others, so it changed too
    assert changed_teams(event) == [1, 2]
    with pytest.raises(FinalizeError, match="changed since finalizing for Team 1, Team 2; finalize again"):
        clear_event(event, "gold")
    assert ServiceScore.objects.count() == 2
    event.refresh_from_db()
    assert event.is_active


def test_clear_names_a_few_changed_teams_and_counts_the_rest(event):
    for number in range(3, 8):
        team = Team.objects.create(team_number=number, team_name=f"Team {number:02d}", is_active=True)
        ServiceScore.objects.create(team=team, service_points=Decimal(10 * number))
        set_team_school(team, f"School {number}", contact_email=f"c{number}@example.edu")
    finalize_open_event(None, "gold")
    ServiceScore.objects.filter(team__team_number__gte=3).update(service_points=Decimal(5))

    with pytest.raises(FinalizeError, match="for Team 1, Team 2, Team 3 and 4 more; finalize again"):
        clear_event(event, "gold")


def test_clear_refuses_an_unfinalized_event(event):
    with pytest.raises(FinalizeError, match="Finalize this event first"):
        clear_event(event, "gold")


def test_clear_reads_the_finalized_flag_under_the_lock_not_from_a_stale_copy(event):
    """The flag is read under the lock, not from the caller's copy of the event."""
    stale = Event.objects.get(pk=event.pk)
    finalize_open_event(None, "gold")
    Event.objects.filter(pk=event.pk).update(is_finalized=False)
    stale.is_finalized = True

    with pytest.raises(FinalizeError, match="Finalize this event first"):
        clear_event(stale, "gold")


def test_clear_deletes_live_data_keeps_results_and_the_school_list_and_closes_the_event(event):
    finalize_open_event(None, "gold")

    wiped = clear_event(event, "gold")

    assert wiped.deleted["ServiceScore"] == 2
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


def test_clear_refuses_while_the_competition_runs(event):
    finalize_open_event(None, "gold")
    CompetitionConfig.objects.update_or_create(pk=1, defaults={"applications_enabled": True})

    with pytest.raises(FinalizeError, match="Competition must be stopped before clearing"):
        clear_event(event, "gold")
    assert ServiceScore.objects.count() == 2


def test_clear_resets_the_competition_times(event):
    now = timezone.now()
    CompetitionConfig.objects.update_or_create(
        pk=1, defaults={"competition_start_time": now, "competition_end_time": now}
    )
    finalize_open_event(None, "gold")

    clear_event(event, "gold")

    config = CompetitionConfig.get_config()
    assert (config.competition_start_time, config.competition_end_time) == (None, None)


def test_clear_rebuilds_the_archive(event):
    finalize_open_event(None, "gold")
    finalized_at = EventArchive.objects.get(event=event).updated_at

    clear_event(event, "gold")

    archive = EventArchive.objects.get(event=event)
    assert archive.updated_at > finalized_at
    with zipfile.ZipFile(io.BytesIO(archive.data)) as zipped:
        names = set(zipped.namelist())
    assert names == {f"{d.filename}.{ext}" for d in DATASETS.values() for ext in ("csv", "json")}


def test_a_cleared_event_cannot_be_cleared_again(event):
    finalize_open_event(None, "gold")
    clear_event(event, "gold")

    with pytest.raises(FinalizeError, match="not open"):
        clear_event(event, "gold")


def test_clear_records_the_ticket_counter_reset_apart_from_the_deletions(event):
    Team.objects.filter(team_number=1).update(ticket_counter=85)
    finalize_open_event(None, "gold")

    clear_event(event, "gold")

    details = AuditLog.objects.get(action="event_cleared").details
    assert details["ticket_counters_reset"] == 1
    assert "TeamTicketCounter" not in details["deleted_counts"]
    assert details["total_deleted"] == 2


def test_after_clearing_a_replace_import_opens_the_next_event(event):
    finalize_open_event(None, "gold")
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

    ServiceScore.objects.create(team=first, service_points=Decimal(100))

    outcome = finalize_open_event(NewEvent("Invitational 1", date(2026, 10, 3), "invitational"), "gold")

    event = outcome.event
    assert outcome.results == 2
    assert event.is_active and event.is_finalized
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


def test_transition_refuses_without_an_active_season():
    Season.objects.create(name="2025", year=2025, is_active=False)
    ServiceScore.objects.create(team=Team.objects.create(team_number=1, team_name="Team 01"), service_points=1)

    with pytest.raises(FinalizeError, match="active season"):
        finalize_open_event(NewEvent("Invitational 1", date(2026, 10, 3), "invitational"), "gold")
    assert not Event.objects.exists()


def test_a_failure_while_finalizing_a_new_event_writes_nothing():
    Season.objects.create(name="2026", year=2026, is_active=True)
    team = Team.objects.create(team_number=1, team_name="Team 01", is_active=True)
    set_team_school(team, "Example University", contact_email="c@example.edu")
    ServiceScore.objects.create(team=team, service_points=Decimal(100))

    with (
        patch("registration.services.export_all_zip_bytes", side_effect=RuntimeError("disk full")),
        pytest.raises(RuntimeError),
    ):
        finalize_open_event(NewEvent("Invitational 1", date(2026, 10, 3), "invitational"), "gold")

    assert not Event.objects.exists()
    assert not EventResult.objects.exists()
    assert not EventArchive.objects.exists()


def test_after_clearing_finalize_does_not_make_an_event_for_the_old_list(event):
    finalize_open_event(None, "gold")
    clear_event(event, "gold")

    with pytest.raises(FinalizeError, match="Nothing to finalize: no live scores"):
        finalize_open_event(NewEvent("Invitational 2", date(2026, 11, 3), "invitational"), "gold")
    assert list(Event.objects.all()) == [event]
    assert EventResult.objects.exclude(event=event).count() == 0


@pytest.mark.django_db(transaction=True)
def test_two_finalizes_at_once_both_succeed(event):
    errors: list[Exception] = []

    def run():
        try:
            finalize_open_event(None, "gold")
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
