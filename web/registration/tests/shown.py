"""Test helpers: a new event as a page posts it."""

from datetime import date

from registration.models import Event, Season
from registration.services import NewEvent, NewSeason, close_out_card_check
from registration.stage import event_stage


def new_event(name: str, day: date, event_type: str, season: NewSeason | None = None) -> NewEvent:
    """A NewEvent as a page would post it now: with the id of the season it shows as active."""
    active = Season.objects.active()
    return NewEvent(name, day, event_type, active.pk if active else None, season)


def close_out_preview(event: Event | None) -> str:
    stage = event_stage()
    assert stage.event == event
    return close_out_card_check(stage).preview_key(event)
