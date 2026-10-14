"""A new event with no active season comes with a new season, created as the only active one under the lock; the
import and Close Out check a new event the same way (new_event_problem)."""

from datetime import date

import pytest

from registration.models import Event, Season
from registration.services import (
    ImportBlockedError,
    NewEvent,
    NewSeason,
    SchoolListRow,
    import_school_list,
    new_event_problem,
)
from registration.tests.shown import new_event
from team.models import SchoolInfo, Team

pytestmark = pytest.mark.django_db

ROWS = [SchoolListRow(team_number=1, school_name="Example University", contact_email="c@example.edu")]


def _in_new_season():
    return new_event("Invitational 1", date(2026, 10, 3), "invitational", NewSeason("2026", 2026))


def test_an_import_into_a_new_season_creates_it_as_the_only_active_one():
    Season.objects.create(name="2025", year=2025)
    Team.objects.create(team_number=1, team_name="Team 01")

    import_school_list(_in_new_season(), ROWS, "gold")

    season = Season.objects.get(year=2026)
    assert list(Season.objects.filter(is_active=True)) == [season]
    event = Event.objects.open()
    assert (event.name, event.season, event.registration_open) == ("Invitational 1", season, False)


@pytest.mark.parametrize(
    ("existing", "message"),
    [
        (lambda: Season.objects.create(name="Other", year=2030, is_active=True), "The active season changed"),
        (lambda: Season.objects.create(name="Old", year=2026, is_active=False), "already has the year 2026"),
    ],
)
def test_an_import_into_a_new_season_refuses_and_writes_nothing(existing, message):
    shown = _in_new_season()
    existing()
    Team.objects.create(team_number=1, team_name="Team 01")

    with pytest.raises(ImportBlockedError, match=message):
        import_school_list(shown, ROWS, "gold")
    assert Season.objects.count() == 1
    assert not Event.objects.exists()
    assert not SchoolInfo.objects.exists()


def test_a_new_event_needs_a_season():
    assert new_event_problem(new_event("Inv", date(2026, 10, 3), "invitational")) == (
        "No season is active: give the new season a name and a year."
    )


def test_a_new_event_name_must_be_free_in_the_active_season():
    season = Season.objects.create(name="2026", year=2026, is_active=True)
    Event.objects.create(season=season, name="Inv", event_type="qualifier", date=date(2026, 9, 1))
    Team.objects.create(team_number=1, team_name="Team 01")

    with pytest.raises(ImportBlockedError, match="already has an event named Inv"):
        import_school_list(new_event("Inv", date(2026, 10, 3), "invitational"), ROWS, "gold")
    assert Event.objects.count() == 1


def test_an_import_can_start_a_new_season_while_one_is_active():
    Season.objects.create(name="2026", year=2026, is_active=True)
    Team.objects.create(team_number=1, team_name="Team 01")
    rollover = new_event("Invitational 1", date(2027, 10, 2), "invitational", NewSeason("2027", 2027))

    import_school_list(rollover, ROWS, "gold")

    season = Season.objects.get(year=2027)
    assert list(Season.objects.filter(is_active=True)) == [season]
    assert Event.objects.open().season == season


def test_without_a_new_season_the_active_one_holds_the_new_event():
    active = Season.objects.create(name="2026", year=2026, is_active=True)
    Team.objects.create(team_number=1, team_name="Team 01")

    import_school_list(new_event("Invitational 1", date(2026, 10, 3), "invitational"), ROWS, "gold")

    assert Event.objects.open().season == active
    assert list(Season.objects.all()) == [active]


def test_a_new_season_refuses_when_the_active_season_is_not_the_one_the_page_showed():
    shown = Season.objects.create(name="2026", year=2026)
    Season.objects.create(name="Other", year=2030, is_active=True)

    problem = new_event_problem(NewEvent("Inv", date(2027, 10, 2), "invitational", shown.pk, NewSeason("2027", 2027)))

    assert problem == "The active season changed since this page loaded. Reload the page."


def test_a_plain_new_event_refuses_when_the_active_season_changed_since_the_page_loaded():
    Season.objects.create(name="2026", year=2026, is_active=True)
    Team.objects.create(team_number=1, team_name="Team 01")
    shown = new_event("Invitational 1", date(2026, 10, 3), "invitational")
    Season.objects.create(name="2027", year=2027).activate()

    with pytest.raises(ImportBlockedError, match="The active season changed since this page loaded"):
        import_school_list(shown, ROWS, "gold")
    assert not Event.objects.exists()


def test_the_active_seasons_own_year_says_to_leave_the_new_season_empty():
    Season.objects.create(name="2026", year=2026, is_active=True)

    problem = new_event_problem(new_event("Inv", date(2026, 10, 3), "invitational", NewSeason("2026-27", 2026)))

    assert problem == "2026 is the year of the active season, 2026: leave the new season empty to put the event in it."
