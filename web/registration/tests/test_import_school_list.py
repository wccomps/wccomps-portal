"""The import re-checks the event before writing: a refused import writes nothing, and a new event is saved and
opened only when the import goes ahead."""

from datetime import date

import pytest

from registration.models import Event, Season
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
