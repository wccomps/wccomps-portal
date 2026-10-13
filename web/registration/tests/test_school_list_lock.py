"""The school list lock is taken by the registration services' writes, Event.open and Event.close, the start of a
first packet send and the resend checks, each before it reads the open event."""

from datetime import date
from unittest.mock import patch

import pytest
from django.contrib.auth.models import User
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from packets.models import Packet, PacketDistribution
from packets.services import PacketDistributionService, begin_distribution
from scoring.models import ServiceScore

from core.models import UserGroups
from registration.models import Event, Season
from registration.services import (
    NewEvent,
    SchoolListRow,
    add_to_school_list,
    clear_event,
    clear_school_list,
    finalize_open_event,
    import_school_list,
    remove_team_school,
    set_team_school,
)
from team.models import Team

pytestmark = pytest.mark.django_db


@pytest.fixture
def team():
    return Team.objects.create(team_number=1, team_name="Team 01")


def _event(open_=True):
    season = Season.objects.create(name="2026", year=2026, is_active=True)
    return Event.objects.create(
        season=season, name="Invitational", event_type="invitational", date=date(2026, 10, 3), is_active=open_
    )


def _assert_locks_before_reading_the_open_event(queries):
    sql = [query["sql"] for query in queries.captured_queries]
    lock = next(i for i, q in enumerate(sql) if "pg_advisory_xact_lock" in q)
    check = next(i for i, q in enumerate(sql) if '"registration_event"."is_active"' in q)
    assert lock < check


@pytest.mark.parametrize("with_event", [True, False])
def test_setting_a_school_takes_the_lock(team, with_event):
    if with_event:
        _event()
    with CaptureQueriesContext(connection) as queries:
        set_team_school(team, "Example University", contact_email="c@example.edu")

    _assert_locks_before_reading_the_open_event(queries)


def test_removing_a_school_takes_the_lock(team):
    _event()
    set_team_school(team, "Example University", contact_email="c@example.edu")

    with CaptureQueriesContext(connection) as queries:
        remove_team_school(team)

    _assert_locks_before_reading_the_open_event(queries)


def test_clearing_the_list_takes_the_lock_with_no_event_or_season(team):
    set_team_school(team, "Example University", contact_email="c@example.edu")

    with CaptureQueriesContext(connection) as queries:
        clear_school_list()

    _assert_locks_before_reading_the_open_event(queries)


def test_the_import_takes_the_lock_before_checking_the_event(team):
    event = _event(open_=False)

    with CaptureQueriesContext(connection) as queries:
        import_school_list(
            event, [SchoolListRow(team_number=1, school_name="Example University", contact_email="c@e.edu")], "gold"
        )

    _assert_locks_before_reading_the_open_event(queries)
    assert event.team_assignments.get().team == team


def test_starting_a_packet_send_takes_the_lock():
    _event()
    packet = Packet.objects.create(
        title="Packet", file_data=b"x", filename="p.pdf", mime_type="application/pdf", file_size=1, uploaded_by="gold"
    )

    with CaptureQueriesContext(connection) as queries:
        begin_distribution(packet)

    _assert_locks_before_reading_the_open_event(queries)


def test_starting_a_packet_send_creates_its_distributions_under_the_lock(team):
    _event()
    set_team_school(team, "Example University", contact_email="c@example.edu")
    Team.objects.filter(pk=team.pk).update(is_active=True)
    packet = Packet.objects.create(
        title="Packet", file_data=b"x", filename="p.pdf", mime_type="application/pdf", file_size=1, uploaded_by="gold"
    )

    with CaptureQueriesContext(connection) as queries:
        distributions = begin_distribution(packet)

    assert [dist.team for dist in distributions] == [team]
    assert list(PacketDistribution.objects.values_list("pk", flat=True)) == [distributions[0].pk]
    sql = [query["sql"] for query in queries.captured_queries]
    lock = next(i for i, q in enumerate(sql) if "pg_advisory_xact_lock" in q)
    create = next(i for i, q in enumerate(sql) if q.startswith('INSERT INTO "packets_packetdistribution"'))
    assert lock < create


def test_opening_an_event_takes_the_lock():
    event = _event(open_=False)

    with CaptureQueriesContext(connection) as queries:
        event.open()

    _assert_locks_before_reading_the_open_event(queries)


def test_closing_an_event_takes_the_lock():
    event = _event()

    with CaptureQueriesContext(connection) as queries:
        event.close()

    sql = [query["sql"] for query in queries.captured_queries]
    lock = next(i for i, q in enumerate(sql) if "pg_advisory_xact_lock" in q)
    write = next(i for i, q in enumerate(sql) if q.startswith('UPDATE "registration_event"'))
    assert lock < write
    event.refresh_from_db()
    assert not event.is_active


def test_finalizing_takes_the_lock_before_reading_the_open_event(team):
    _event()
    set_team_school(team, "Example University", contact_email="c@example.edu")

    with CaptureQueriesContext(connection) as queries:
        finalize_open_event(None, "gold")

    _assert_locks_before_reading_the_open_event(queries)


def test_clearing_takes_the_lock_before_reading_the_open_event(team):
    event = _event()
    set_team_school(team, "Example University", contact_email="c@example.edu")
    finalize_open_event(None, "gold")

    with CaptureQueriesContext(connection) as queries:
        clear_event(event, "gold")

    _assert_locks_before_reading_the_open_event(queries)


def test_opening_an_event_for_the_current_schools_takes_the_lock(team):
    Season.objects.create(name="2026", year=2026, is_active=True)
    set_team_school(team, "Example University", contact_email="c@example.edu")

    ServiceScore.objects.create(team=team, service_points=1)

    with CaptureQueriesContext(connection) as queries:
        finalize_open_event(NewEvent("Invitational", date(2026, 10, 3), "invitational"), "gold")

    _assert_locks_before_reading_the_open_event(queries)


def _sent_packet(team):
    event = _event()
    set_team_school(team, "Example University", contact_email="c@example.edu")
    packet = Packet.objects.create(
        title="Packet",
        file_data=b"x",
        filename="p.pdf",
        mime_type="application/pdf",
        file_size=1,
        uploaded_by="gold",
        status="completed",
        event=event,
    )
    PacketDistribution.objects.create(packet=packet, team=team, email_status="failed")
    return packet


@patch.object(PacketDistributionService, "send_packet_email")
def test_resending_to_a_team_takes_the_lock_before_its_check_and_contact_write(_send, team):
    packet = _sent_packet(team)

    with CaptureQueriesContext(connection) as queries:
        PacketDistributionService().resend_to_team(packet, team, "new@example.edu")

    _assert_locks_before_reading_the_open_event(queries)
    sql = [query["sql"] for query in queries.captured_queries]
    lock = next(i for i, q in enumerate(sql) if "pg_advisory_xact_lock" in q)
    contact_write = next(i for i, q in enumerate(sql) if q.startswith('UPDATE "team_schoolinfo"'))
    assert lock < contact_write


@patch.object(PacketDistributionService, "send_packet_email")
def test_resending_failed_emails_takes_the_lock(_send, team):
    packet = _sent_packet(team)

    with CaptureQueriesContext(connection) as queries:
        list(PacketDistributionService().stream_resend_failed(packet))

    _assert_locks_before_reading_the_open_event(queries)


@patch.object(PacketDistributionService, "send_packet_email")
def test_the_admin_retry_takes_the_lock(_send, team):
    packet = _sent_packet(team)
    admin = User.objects.create(username="admin", is_staff=True, is_superuser=True)
    UserGroups.objects.update_or_create(user=admin, defaults={"groups": ["WCComps_Discord_Admin"], "authentik_id": "x"})
    client = Client()
    client.force_login(admin)

    with CaptureQueriesContext(connection) as queries:
        client.post(
            reverse("admin:packets_packetdistribution_changelist"),
            {"action": "retry_failed_emails", "_selected_action": [packet.distributions.get().pk]},
        )

    _assert_locks_before_reading_the_open_event(queries)
    assert _send.called


def test_adding_schools_takes_the_lock(team):
    event = _event()

    with CaptureQueriesContext(connection) as queries:
        add_to_school_list(event, [SchoolListRow(1, "Example University", "c@example.edu")], "gold")

    _assert_locks_before_reading_the_open_event(queries)


@patch.object(PacketDistributionService, "send_packet_email")
def test_sending_to_new_teams_creates_their_distributions_under_the_lock(_send, team):
    packet = _sent_packet(team)
    set_team_school(
        Team.objects.create(team_number=2, team_name="Team 02"), "School Two", contact_email="c@example.edu"
    )

    with CaptureQueriesContext(connection) as queries:
        list(PacketDistributionService().stream_send_to_new_teams(packet))

    _assert_locks_before_reading_the_open_event(queries)
    sql = [query["sql"] for query in queries.captured_queries]
    lock = next(i for i, q in enumerate(sql) if "pg_advisory_xact_lock" in q)
    status_read = 'SELECT "packets_packet"."id", "packets_packet"."status"'
    status = next(i for i, q in enumerate(sql) if q.startswith(status_read))
    create = next(i for i, q in enumerate(sql) if q.startswith('INSERT INTO "packets_packetdistribution"'))
    assert lock < status < create
