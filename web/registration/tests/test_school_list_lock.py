"""Every school-list write and packet send takes the same lock, then reads the open event."""

from datetime import date

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from packets.models import Packet
from packets.services import begin_distribution

from registration.models import Event, Season
from registration.services import (
    SchoolListRow,
    clear_school_list,
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
