"""Close Out Event freezes the results, deletes the live data, closes the event and queues the Discord cleanup in one
transaction: all of it or nothing."""

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
from scoring.models import InjectScore, QuotientMetadataCache, RedTeamScore, ServiceScore

from core.models import AuditLog, CompetitionConfig, DiscordTask, QueuedAnnouncement
from registration.models import Event, EventArchive, EventResult, School, Season
from registration.services import (
    CloseOutError,
    NewEvent,
    SchoolListRow,
    clear_leftover_data,
    close_out_event,
    import_problem,
    import_school_list,
    set_team_school,
)
from registration.stage import Stage, event_stage
from team.models import DiscordLink, SchoolInfo, Team

pytestmark = pytest.mark.django_db

OCT_3 = NewEvent("Invitational 1", date(2026, 10, 3), "invitational")


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


def _cleanups():
    return list(DiscordTask.objects.filter(task_type="cleanup_competition"))


def _assert_nothing_written(event=None, scores=2):
    assert not EventResult.objects.exists()
    assert not EventArchive.objects.exists()
    assert _cleanups() == []
    assert not AuditLog.objects.filter(action="event_closed_out").exists()
    assert ServiceScore.objects.count() == scores
    if event is not None:
        event.refresh_from_db()
        assert event.is_active and not event.is_finalized


def test_close_out_freezes_wipes_closes_and_queues_the_cleanup(event):
    now = timezone.now()
    CompetitionConfig.objects.update_or_create(
        pk=1, defaults={"competition_start_time": now, "competition_end_time": now, "start_message": "VM: admin/pw"}
    )
    QuotientMetadataCache.objects.create()
    QueuedAnnouncement.objects.create(team=Team.objects.get(team_number=1), message="Hi", sender_name="gold")
    _link_team(Team.objects.get(team_number=1))

    outcome = close_out_event(event, "gold")

    assert outcome.results == 2
    results = list(EventResult.objects.filter(event=event))
    assert [(r.school.name, r.team_number, r.rank) for r in results] == [("School 1", 1, 1), ("School 2", 2, 2)]
    assert results[0].total_score == Decimal(300)
    assert outcome.wiped.deleted["ServiceScore"] == 2
    assert not ServiceScore.objects.exists()
    event.refresh_from_db()
    assert not event.is_active and event.is_finalized
    assert Event.objects.open() is None
    # The school list and the event's assignments stay
    assert SchoolInfo.objects.count() == 2
    assert event.team_assignments.count() == 2
    [cleanup] = _cleanups()
    # The bot does only the Discord side for a close-out: every database reset is in this transaction
    assert cleanup.payload == {"requested_by": "gold", "closed_out_event_id": event.pk}
    config = CompetitionConfig.get_config()
    assert (config.competition_start_time, config.competition_end_time, config.start_message) == (None, None, "")
    assert not QuotientMetadataCache.objects.exists()
    assert not QueuedAnnouncement.objects.exists()
    log = AuditLog.objects.get(action="event_closed_out")
    assert log.target_id == event.pk
    assert log.details["results"] == 2
    assert log.details["deleted_counts"] == {"ServiceScore": 2, "BlueTeamDiscordLink": 1}
    assert log.details["queued_announcements_deleted"] == 1
    assert not AuditLog.objects.filter(action__in=["event_finalized", "event_cleared"]).exists()


def test_the_archive_is_taken_before_the_wipe(event):
    close_out_event(event, "gold")

    with zipfile.ZipFile(io.BytesIO(EventArchive.objects.get(event=event).data)) as zipped:
        names = set(zipped.namelist())
        final_scores = zipped.read(f"{DATASETS['final_scores'].filename}.csv").decode()
    assert names == {f"{d.filename}.{ext}" for d in DATASETS.values() for ext in ("csv", "json")}
    # Ranked from the live scores: after the wipe no team has a rank
    assert final_scores.splitlines()[1].startswith("1,")


def test_results_follow_the_school_list(event):
    set_team_school(Team.objects.get(team_number=2), "Example University", contact_email="c@example.edu")

    close_out_event(event, "gold")

    assert EventResult.objects.get(event=event, team_number=2).school.name == "Example University"


def test_an_event_finalized_before_is_frozen_again_from_the_live_scores(event):
    """Finalize used to be its own step; an event finalized then and still open is closed out as it stands now."""
    Event.objects.filter(pk=event.pk).update(is_finalized=True)
    EventResult.objects.create(
        event=event, school=School.for_name("School 1"), team_number=1, rank=2, total_score=1, scorecard={}, version=1
    )

    close_out_event(event, "gold")

    assert list(EventResult.objects.values_list("team_number", "rank")) == [(1, 1), (2, 2)]


@pytest.mark.parametrize(
    ("setup", "message"),
    [
        (
            lambda event: CompetitionConfig.objects.update_or_create(pk=1, defaults={"applications_enabled": True}),
            "^Stop the competition first.$",
        ),
        (lambda event: Team.objects.filter(team_number=2).update(is_active=False), "Reactivate Team 2"),
        (
            lambda event: InjectScore.objects.create(
                team=Team.objects.create(team_number=3, team_name="Team 03"),
                inject_id="1",
                inject_name="Memo",
                points_awarded=Decimal(5),
            ),
            "Team 3 has scores but no school",
        ),
        (lambda event: event.team_assignments.all().delete(), "no schools on its list"),
    ],
    ids=["running", "inactive-team", "scored-team-without-school", "no-schools"],
)
def test_a_refusal_writes_nothing(event, setup, message):
    setup(event)
    scores = ServiceScore.objects.count()

    with pytest.raises(CloseOutError, match=message):
        close_out_event(event, "gold")

    _assert_nothing_written(event, scores)


def test_a_red_finding_against_a_team_with_no_school_is_refused(event):
    unlisted = Team.objects.create(team_number=3, team_name="Team 03", is_active=True)
    finding = RedTeamScore.objects.create(
        attack_vector="ssh", points_per_team=Decimal(10), submitted_by=User.objects.create_user("red")
    )
    finding.affected_teams.add(unlisted)

    with pytest.raises(CloseOutError, match="Team 3 has scores but no school"):
        close_out_event(event, "gold")
    _assert_nothing_written(event)


def test_an_event_closed_since_the_page_loaded_is_refused(event):
    Event.objects.filter(pk=event.pk).update(is_active=False)

    with pytest.raises(CloseOutError, match="is no longer open. Reload the page"):
        close_out_event(event, "gold")
    assert not EventResult.objects.exists()
    assert _cleanups() == []
    assert ServiceScore.objects.count() == 2


def test_a_new_event_is_refused_once_an_event_is_open(event):
    with pytest.raises(CloseOutError, match="was opened since the page loaded. Reload the page"):
        close_out_event(NewEvent("Invitational 2", date(2026, 11, 3), "invitational"), "gold")
    assert Event.objects.count() == 1
    _assert_nothing_written(event)


@pytest.mark.parametrize("failing", ["registration.services.wipe_competition_data", "core.models.DiscordTask.enqueue"])
def test_a_failure_after_the_freeze_writes_nothing(event, failing):
    with patch(failing, side_effect=RuntimeError("boom")), pytest.raises(RuntimeError):
        close_out_event(event, "gold")

    _assert_nothing_written(event)


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


def test_with_no_event_open_it_creates_and_closes_the_event_for_the_current_school_list():
    """The Oct 3 event ran without an import: no event was ever opened or finalized."""
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
    assert event_stage().stage == Stage.UNRECORDED

    outcome = close_out_event(OCT_3, "gold")

    event = outcome.event
    assert outcome.results == 2
    event.refresh_from_db()
    assert not event.is_active and event.is_finalized
    assert event.season == season
    assert (event.name, event.date, event.event_type) == ("Invitational 1", date(2026, 10, 3), "invitational")
    assert not event.registration_open
    assert sorted(event.team_assignments.values_list("team__team_number", "school__name")) == [
        (5, "Example University"),
        (6, "School Two"),
    ]
    for packet, owner in ((sent, event), (sending, event), (draft, None), (stamped, earlier)):
        packet.refresh_from_db()
        assert packet.event == owner
    assert not ServiceScore.objects.exists()
    assert len(_cleanups()) == 1
    assert SchoolInfo.objects.get(team=first).school_name == "Example University"
    assert event_stage().stage == Stage.IDLE


def test_with_no_event_open_a_failure_writes_nothing():
    Season.objects.create(name="2026", year=2026, is_active=True)
    team = Team.objects.create(team_number=1, team_name="Team 01", is_active=True)
    set_team_school(team, "Example University", contact_email="c@example.edu")
    ServiceScore.objects.create(team=team, service_points=Decimal(100))

    with (
        patch("registration.services.wipe_competition_data", side_effect=RuntimeError("boom")),
        pytest.raises(RuntimeError),
    ):
        close_out_event(OCT_3, "gold")

    assert not Event.objects.exists()
    _assert_nothing_written(scores=1)


def test_with_no_event_open_it_needs_an_active_season():
    Season.objects.create(name="2025", year=2025, is_active=False)
    team = Team.objects.create(team_number=1, team_name="Team 01", is_active=True)
    set_team_school(team, "Example University", contact_email="c@example.edu")
    ServiceScore.objects.create(team=team, service_points=1)

    with pytest.raises(CloseOutError, match="No season is active"):
        close_out_event(OCT_3, "gold")
    assert not Event.objects.exists()
    _assert_nothing_written(scores=1)


def test_with_no_event_open_a_name_used_in_the_season_is_refused():
    season = Season.objects.create(name="2026", year=2026, is_active=True)
    Event.objects.create(season=season, name="Invitational 1", event_type="qualifier", date=date(2026, 9, 1))
    team = Team.objects.create(team_number=1, team_name="Team 01", is_active=True)
    set_team_school(team, "Example University", contact_email="c@example.edu")
    ServiceScore.objects.create(team=team, service_points=1)

    with pytest.raises(CloseOutError, match="already has an event named Invitational 1"):
        close_out_event(OCT_3, "gold")
    assert Event.objects.count() == 1
    _assert_nothing_written(scores=1)


def test_with_no_event_open_and_no_live_data_there_is_nothing_to_close_out(event):
    close_out_event(event, "gold")

    with pytest.raises(CloseOutError, match="Nothing to close out"):
        close_out_event(NewEvent("Invitational 2", date(2026, 11, 3), "invitational"), "gold")
    assert list(Event.objects.all()) == [event]
    assert len(_cleanups()) == 1


def _link_team(team):
    user = User.objects.create_user(f"blue{team.team_number}")
    DiscordLink.objects.create(team=team, discord_id=team.team_number, discord_username="blue", user=user)


def test_an_open_event_with_no_scores_is_refused_even_once_teams_linked(event):
    """Right after an import: one confirmation would freeze an all-zero event for good and tear down Discord."""
    ServiceScore.objects.all().delete()
    _link_team(Team.objects.get(team_number=1))

    assert event_stage().stage == Stage.READY
    with pytest.raises(CloseOutError, match="No team has scores yet"):
        close_out_event(event, "gold")
    _assert_nothing_written(event, scores=0)


def _closed_out_with_a_late_score(event):
    close_out_event(event, "gold")
    ServiceScore.objects.create(team=Team.objects.get(team_number=1), service_points=Decimal(7))


def test_scores_after_a_close_out_can_be_recorded_at_a_new_event(event):
    """An event that ran without an import after the first close-out is not leftovers."""
    _closed_out_with_a_late_score(event)

    assert event_stage().stage == Stage.UNRECORDED
    outcome = close_out_event(NewEvent("Invitational 2", date(2026, 11, 3), "invitational"), "gold")

    assert outcome.results == 2
    assert EventResult.objects.get(event=outcome.event, team_number=1).total_score > 0
    assert len(_cleanups()) == 2


def test_clearing_unrecorded_scores_after_a_close_out_keeps_the_schedule_and_lets_the_import_through(event):
    _closed_out_with_a_late_score(event)
    Team.objects.filter(team_number=2).update(ticket_counter=4)
    now = timezone.now()
    CompetitionConfig.objects.update_or_create(
        pk=1, defaults={"competition_start_time": now, "competition_end_time": now}
    )
    new = NewEvent("Qual", date(2026, 11, 7), "qualifier")
    assert "not recorded at any event yet" in import_problem(new)

    wiped = clear_leftover_data("gold")

    assert wiped.deleted["ServiceScore"] == 1
    assert wiped.ticket_counters_reset == 1
    assert event_stage().stage == Stage.IDLE
    # A start may already be scheduled for the next event
    config = CompetitionConfig.get_config()
    assert (config.competition_start_time, config.competition_end_time) == (now, now)
    details = AuditLog.objects.get(action="leftover_data_cleared").details
    assert details["deleted_counts"] == {"ServiceScore": 1}
    assert details["ticket_counters_reset"] == 1
    assert EventResult.objects.filter(event=event).count() == 2
    rows = [SchoolListRow(team_number=2, school_name="Example University", contact_email="c@example.edu")]
    import_school_list(new, rows, "gold")
    assert Event.objects.open().name == "Qual"


def test_live_data_with_no_scores_is_leftovers_to_clear(event):
    Event.objects.filter(pk=event.pk).update(is_active=False)
    ServiceScore.objects.all().delete()
    _link_team(Team.objects.get(team_number=1))

    assert event_stage().stage == Stage.LEFTOVERS
    assert "Clear leftover data" in import_problem(NewEvent("Qual", date(2026, 11, 7), "qualifier"))
    with pytest.raises(CloseOutError, match="Nothing to close out"):
        close_out_event(NewEvent("Qual", date(2026, 11, 7), "qualifier"), "gold")

    assert clear_leftover_data("gold").deleted["BlueTeamDiscordLink"] == 1
    assert event_stage().stage == Stage.IDLE


def test_clearing_leftovers_is_refused_while_the_competition_runs(event):
    _closed_out_with_a_late_score(event)
    CompetitionConfig.objects.update_or_create(pk=1, defaults={"applications_enabled": True})

    with pytest.raises(CloseOutError, match="Competition must be stopped before clearing"):
        clear_leftover_data("gold")
    assert ServiceScore.objects.count() == 1


def test_with_an_event_open_its_data_is_not_leftovers(event):
    assert event_stage().stage == Stage.PLAYED
    with pytest.raises(CloseOutError, match="Close Out Event"):
        clear_leftover_data("gold")
    assert ServiceScore.objects.count() == 2


def test_after_a_close_out_a_replace_import_opens_the_next_event(event):
    new = NewEvent("Qual", date(2026, 11, 7), "qualifier")
    assert "Close Out Event" in import_problem(new)
    close_out_event(event, "gold")
    rows = [SchoolListRow(team_number=2, school_name="Example University", contact_email="c@example.edu")]

    assert import_problem(new) == ""
    import_school_list(new, rows, "gold")

    opened = Event.objects.open()
    assert opened.name == "Qual"
    assert list(opened.team_assignments.values_list("school__name", flat=True)) == ["Example University"]
    assert EventResult.objects.filter(event=event).count() == 2
    assert event.team_assignments.count() == 2


def test_scores_never_recorded_point_the_import_at_close_out_and_cannot_be_cleared(event):
    Event.objects.filter(pk=event.pk).update(is_active=False)

    assert event_stage().stage == Stage.UNRECORDED
    assert "Close Out Event" in import_problem(NewEvent("Qual", date(2026, 11, 7), "qualifier"))
    with pytest.raises(CloseOutError, match="record them with Close Out Event"):
        clear_leftover_data("gold")
    assert ServiceScore.objects.count() == 2


@pytest.mark.django_db(transaction=True)
def test_two_close_outs_at_once_close_it_out_once(event):
    errors: list[Exception] = []

    def run():
        try:
            close_out_event(event, "gold")
        except Exception as e:  # noqa: BLE001
            errors.append(e)
        finally:
            connection.close()

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(errors) == 1 and isinstance(errors[0], CloseOutError)
    assert "no longer open" in str(errors[0])
    assert EventResult.objects.filter(event=event).count() == 2
    assert len(_cleanups()) == 1
