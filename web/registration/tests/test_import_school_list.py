"""Importing a school list is one operation: imports are serialized and the event is re-checked under the lock."""

from datetime import date

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from registration.models import Event, EventTeamAssignment, Season
from registration.services import ImportBlockedError, import_school_list
from team.models import SchoolInfo, Team

pytestmark = pytest.mark.django_db


@pytest.fixture
def season():
    return Season.objects.create(name="2026", year=2026, is_active=True)


def _event(season, name, active=False):
    return Event.objects.create(
        season=season, name=name, event_type="invitational", date=date(2026, 10, 3), is_active=active
    )


def _rows():
    team = Team.objects.create(team_number=1, team_name="Team 01")
    return [{"_team": team, "school_name": "Example University", "contact_email": "c@example.edu"}]


def test_the_import_locks_the_season_before_checking_the_event(season):
    event = _event(season, "Invitational")
    rows = _rows()

    with CaptureQueriesContext(connection) as queries:
        import_school_list(event, rows, "gold")

    sql = [query["sql"] for query in queries.captured_queries]
    lock = next(i for i, q in enumerate(sql) if "FOR UPDATE" in q and "registration_season" in q)
    check = next(i for i, q in enumerate(sql) if '"registration_event"."is_active"' in q)
    assert lock < check
    assert SchoolInfo.objects.get().school == EventTeamAssignment.objects.get(event=event).school


def test_the_import_refuses_under_the_lock_when_another_event_opened_first(season):
    _event(season, "First", active=True)
    second = _event(season, "Second")
    rows = _rows()

    with pytest.raises(ImportBlockedError, match=r"First \(2026\) is still open"):
        import_school_list(second, rows, "gold")

    assert not SchoolInfo.objects.exists()
    assert list(Event.objects.filter(is_active=True).values_list("name", flat=True)) == ["First"]


def test_a_new_event_is_saved_only_when_the_import_goes_ahead(season):
    _event(season, "First", active=True)
    new = Event(season=season, name="New", event_type="qualifier", date=date(2026, 11, 7), registration_open=False)
    rows = _rows()

    with pytest.raises(ImportBlockedError):
        import_school_list(new, rows, "gold")

    assert not Event.objects.filter(name="New").exists()


def test_a_new_event_is_created_and_opened(season):
    new = Event(season=season, name="New", event_type="qualifier", date=date(2026, 11, 7), registration_open=False)

    counts = import_school_list(new, _rows(), "gold")

    assert counts["created"] == 1
    assert Event.objects.get(is_active=True).name == "New"
