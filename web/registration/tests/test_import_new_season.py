"""An import into a new event with no active season creates the season, as the only active one, under the lock."""

from datetime import date

import pytest

from registration.models import Event, Season
from registration.services import ImportBlockedError, SchoolListRow, import_school_list
from team.models import SchoolInfo, Team

pytestmark = pytest.mark.django_db


def test_an_import_into_a_new_season_creates_it_as_the_only_active_one():
    old = Season.objects.create(name="2025", year=2025, is_active=False)
    Team.objects.create(team_number=1, team_name="Team 01")
    new = Event(
        season=Season(name="2026", year=2026, is_active=True),
        name="Invitational 1",
        event_type="invitational",
        date=date(2026, 10, 3),
        registration_open=False,
    )
    rows = [SchoolListRow(team_number=1, school_name="Example University", contact_email="c@example.edu")]

    import_school_list(new, rows, "gold")

    season = Season.objects.get(year=2026)
    assert Season.objects.active() == season
    old.refresh_from_db()
    assert not old.is_active
    assert Event.objects.open() == new and new.season == season


@pytest.mark.parametrize(
    ("existing", "message"),
    [
        (lambda: Season.objects.create(name="Other", year=2030, is_active=True), "Other season was activated"),
        (lambda: Season.objects.create(name="Old", year=2026, is_active=False), "already has the year 2026"),
    ],
)
def test_an_import_into_a_new_season_refuses_and_writes_nothing(existing, message):
    existing()
    Team.objects.create(team_number=1, team_name="Team 01")
    new = Event(
        season=Season(name="2026", year=2026, is_active=True),
        name="Invitational 1",
        event_type="invitational",
        date=date(2026, 10, 3),
        registration_open=False,
    )
    rows = [SchoolListRow(team_number=1, school_name="Example University", contact_email="c@example.edu")]

    with pytest.raises(ImportBlockedError, match=message):
        import_school_list(new, rows, "gold")
    assert Season.objects.count() == 1
    assert not Event.objects.exists()
    assert not SchoolInfo.objects.exists()
