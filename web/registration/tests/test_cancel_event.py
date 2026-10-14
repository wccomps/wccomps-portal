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
    NewEvent,
    SchoolListRow,
    cancel_event,
    clear_leftover_data,
    delete_event,
    event_delete_problem,
    import_problem,
    import_school_list,
    set_team_school,
)
from registration.stage import STAGE_TEXT, Stage, event_stage
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
    new = NewEvent("Qual", date(2026, 11, 7), "qualifier")
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
            "has scores: close it out instead",
        ),
        (
            lambda event: CompetitionConfig.objects.update_or_create(pk=1, defaults={"applications_enabled": True}),
            "^Stop the competition first, with Stop Competition on the Competition page.$",
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
    assert "Cancel event" in STAGE_TEXT[Stage.READY][1]
    assert "Cancel event" in import_problem(NewEvent("Qual", date(2026, 11, 7), "qualifier"))


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
