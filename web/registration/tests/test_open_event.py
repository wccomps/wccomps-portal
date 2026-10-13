"""At most one event is open, and the database holds that rule."""

from datetime import date

import pytest
from django.db import IntegrityError

from registration.models import Event, Season

pytestmark = pytest.mark.django_db


@pytest.fixture
def season():
    return Season.objects.create(name="2026", year=2026, is_active=True)


def _event(season, name, active=False):
    return Event.objects.create(
        season=season, name=name, event_type="invitational", date=date(2026, 10, 3), is_active=active
    )


def test_a_second_active_event_is_refused_by_the_database(season):
    _event(season, "First", active=True)

    with pytest.raises(IntegrityError):
        _event(season, "Second", active=True)


def test_opening_an_event_closes_the_open_one(season):
    first = _event(season, "First", active=True)
    second = _event(season, "Second")

    second.open()

    assert list(Event.objects.filter(is_active=True)) == [second]
    first.refresh_from_db()
    assert not first.is_active


def test_open_event_is_the_active_event(season):
    event = _event(season, "First", active=True)

    assert Event.objects.open() == event


def test_the_active_event_stays_open_when_its_season_is_not_active(season):
    event = _event(season, "First", active=True)
    season.is_active = False
    season.save()

    assert Event.objects.open() == event
    assert Season.objects.active() is None
