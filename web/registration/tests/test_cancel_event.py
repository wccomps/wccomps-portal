"""Cancel event: the way out for an open event no team has scored (Close Out refuses it). It closes the event and
drops its assignments, keeping the school list and packets."""

from datetime import date
from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext
from packets.models import Packet
from scoring.models import ServiceScore

from core.models import AuditLog, CompetitionConfig
from registration.models import Event, Season
from registration.services import (
    CloseOutError,
    SchoolListRow,
    cancel_event,
    clear_leftover_data,
    close_out_problem,
    delete_event,
    event_delete_problem,
    import_problem,
    import_school_list,
    set_team_school,
)
from registration.stage import Stage, event_stage
from registration.tests.shown import new_event
from team.models import DiscordLink, SchoolInfo, Team

pytestmark = pytest.mark.django_db


@pytest.fixture
def event():
    season = Season.objects.create(name="2026", year=2026, is_active=True)
    event = Event.objects.create(
        season=season, name="Inv", event_type="invitational", date=date(2026, 10, 3), is_active=True
    )
    team = Team.objects.create(team_number=1, team_name="Team 01", is_active=True)
    set_team_school(team, "Example University", contact_email="c@example.edu")
    user = User.objects.create_user("blue1")
    DiscordLink.objects.create(team=team, discord_id=1, discord_username="blue", user=user)
    return event


def test_cancel_closes_the_event_and_drops_its_assignments_keeping_the_list_and_packets(event):
    packet = Packet.objects.create(
        title="Rules",
        file_data=b"x",
        filename="r.pdf",
        mime_type="application/pdf",
        file_size=1,
        uploaded_by="gold",
        status="completed",
        event=event,
    )

    with CaptureQueriesContext(connection) as queries:
        cancel_event(event, "gold")

    sql = [q["sql"] for q in queries.captured_queries]
    lock = next(i for i, q in enumerate(sql) if "pg_advisory_xact_lock" in q)
    read = next(i for i, q in enumerate(sql) if '"registration_event"."is_active"' in q)
    assert lock < read
    event.refresh_from_db()
    assert not event.is_active and not event.is_finalized
    assert not event.team_assignments.exists()
    assert SchoolInfo.objects.count() == 1
    packet.refresh_from_db()
    assert packet.event == event
    log = AuditLog.objects.get(action="event_cancelled")
    assert (log.admin_user, log.target_id, log.details["assignments_deleted"]) == ("gold", event.pk, 1)
    # The linked Discord rows are live data with no scores: Clear leftover data takes them
    assert event_stage().stage == Stage.LEFTOVERS


def test_after_cancelling_the_next_import_goes_through_clear_leftover_data_and_the_event_can_be_deleted(event):
    cancel_event(event, "gold")
    new = new_event("Qual", date(2026, 11, 7), "qualifier")
    assert "Clear leftover data" in import_problem(new)

    clear_leftover_data("gold")
    import_school_list(new, [SchoolListRow(1, "School Two", "c2@example.edu")], "gold")

    assert Event.objects.open().name == "Qual"
    event.refresh_from_db()
    assert event_delete_problem(event) == ""
    delete_event(event)
    assert not Event.objects.filter(pk=event.pk).exists()


@pytest.mark.parametrize(
    ("setup", "message"),
    [
        (
            lambda event: ServiceScore.objects.create(team=Team.objects.get(team_number=1), service_points=Decimal(5)),
            "has scores, so it can't be cancelled",
        ),
        (
            lambda event: CompetitionConfig.objects.update_or_create(pk=1, defaults={"applications_enabled": True}),
            "^The competition is running. When the competition ends, stop it with Stop Competition",
        ),
        (lambda event: Event.objects.filter(pk=event.pk).update(is_active=False), "is no longer open"),
    ],
    ids=["scored", "running", "closed-since"],
)
def test_a_refused_cancel_changes_nothing(event, setup, message):
    setup(event)
    open_before = Event.objects.get(pk=event.pk).is_active

    with pytest.raises(CloseOutError, match=message):
        cancel_event(event, "gold")

    event.refresh_from_db()
    assert event.is_active == open_before
    assert event.team_assignments.count() == 1
    assert not AuditLog.objects.filter(action="event_cancelled").exists()


def test_the_open_event_with_no_scores_names_cancel_as_its_way_out(event):
    assert "Cancel event" in close_out_problem(event_stage())
    assert "Cancel event" in import_problem(new_event("Qual", date(2026, 11, 7), "qualifier"))


def test_an_event_with_sent_packets_is_kept_after_cancelling(event):
    Packet.objects.create(
        title="Rules",
        file_data=b"x",
        filename="r.pdf",
        mime_type="application/pdf",
        file_size=1,
        uploaded_by="gold",
        status="completed",
        event=event,
    )
    cancel_event(event, "gold")
    event.refresh_from_db()

    assert event_delete_problem(event) == "Packets were sent for Inv (2026); they are kept with it."


def test_cancelling_clears_the_start_message(event):
    """It carries the event's credentials."""
    CompetitionConfig.objects.update_or_create(pk=1, defaults={"start_message": "Default VM login"})

    cancel_event(event, "gold")

    assert CompetitionConfig.get_config().start_message == ""


def test_cancelling_resets_for_the_next_event(event):
    """A start scheduled for the cancelled event would start a competition with no event open, and a queued copy of
    its start message would reach the next event's school."""
    from datetime import UTC, datetime

    from core.models import QueuedAnnouncement

    CompetitionConfig.objects.update_or_create(
        pk=1, defaults={"competition_start_time": datetime(2099, 10, 3, tzinfo=UTC), "start_message": "Default login"}
    )
    QueuedAnnouncement.objects.create(team=Team.objects.first(), message="Default login", sender_name="Black Team")

    cancel_event(event, "gold")

    config = CompetitionConfig.get_config()
    assert (config.competition_start_time, config.start_message) == (None, "")
    assert not QueuedAnnouncement.objects.exists()


def test_cancelling_removes_the_start_broadcasts_copies(event):
    """The broadcasts the starts recorded keep the credentials in their payloads (admin, backups, retries), whatever the
    message says now: each is scrubbed, and an unsent one failed so it never goes out. Other broadcasts stay."""
    from core.models import DiscordTask
    from registration.services import START_MESSAGE_SCRUBBED

    start = {"target": "all-teams", "message": "Default login", "sender": "Black Team"}
    unsent = DiscordTask.objects.create(task_type="broadcast_message", payload=start, status="pending")
    sent = DiscordTask.objects.create(task_type="broadcast_message", payload=start, status="completed")
    other = DiscordTask.objects.create(
        task_type="broadcast_message", payload={**start, "message": "Lunch at noon"}, status="pending"
    )
    CompetitionConfig.objects.update_or_create(
        pk=1, defaults={"start_message": "Edited since", "start_broadcasts": [unsent.pk, sent.pk]}
    )

    cancel_event(event, "gold")

    for task, status in ((unsent, "failed"), (sent, "completed")):
        task.refresh_from_db()
        assert (task.status, task.payload["message"]) == (status, START_MESSAGE_SCRUBBED)
    other.refresh_from_db()
    assert (other.status, other.payload["message"]) == ("pending", "Lunch at noon")
    assert CompetitionConfig.get_config().start_broadcasts == []


def test_start_broadcasts_queued_before_the_start_recorded_them_are_scrubbed_or_recorded():
    """By the stored start message or the start's sender and target: sent ones are scrubbed now, an unsent one is left
    for the next reset to scrub and fail. A broadcast someone else sent stays."""
    import importlib

    from django.apps import apps

    from core.models import DiscordTask
    from registration.services import START_MESSAGE_SCRUBBED

    def broadcast(message, status, sender="Black Team", target="all-teams"):
        payload = {"target": target, "message": message, "sender": sender}
        return DiscordTask.objects.create(task_type="broadcast_message", payload=payload, status=status)

    CompetitionConfig.objects.update_or_create(pk=1, defaults={"start_message": "Default login"})
    edited_since = broadcast("Old login", "completed")
    by_text = broadcast("Default login", "failed", sender="gold", target="1-5")
    running = broadcast("Default login", "pending")
    other = broadcast("Lunch at noon", "completed", sender="gold")

    importlib.import_module("core.migrations.0021_scrub_start_broadcasts").scrub_start_broadcasts(apps, None)

    messages = {task.pk: task.payload["message"] for task in DiscordTask.objects.all()}
    assert messages == {
        edited_since.pk: START_MESSAGE_SCRUBBED,
        by_text.pk: START_MESSAGE_SCRUBBED,
        running.pk: "Default login",
        other.pk: "Lunch at noon",
    }
    assert CompetitionConfig.get_config().start_broadcasts == [running.pk]


def test_a_copy_the_bot_queues_during_the_reset_is_deleted_once_it_commits(event, django_capture_on_commit_callbacks):
    """The bot may commit one the reset's snapshot didn't see; the reset deletes again after its own commit."""
    from core.models import QueuedAnnouncement

    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        cancel_event(event, "gold")
    QueuedAnnouncement.objects.create(team=Team.objects.first(), message="Default login", sender_name="Black Team")
    for callback in callbacks:
        callback()

    assert not QueuedAnnouncement.objects.exists()
