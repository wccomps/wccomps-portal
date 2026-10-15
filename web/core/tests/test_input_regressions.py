"""Inputs the property tests found breaking the portal, each pinned with @example of its minimal failing input."""

from datetime import timedelta

from hypothesis import example, given
from hypothesis import strategies as st

from core.admin_views.competition import TIMEZONE_CHOICES
from core.tests.strategies import datetime_text, request_text
from core.utils import parse_datetime_to_utc


@given(when=datetime_text(), tz=st.one_of(st.sampled_from([tz for tz, _ in TIMEZONE_CHOICES]), request_text(40)))
@example(when="0001-01-01T00:00", tz="Asia/Tokyo")
@example(when="9999-12-31T23:59", tz="America/Los_Angeles")
def test_schedule_time_parses_to_utc_or_raises_value_error(when: str, tz: str) -> None:
    """A schedule time at either end of the calendar, shifted past it by its timezone, is refused, not a 500."""
    try:
        result = parse_datetime_to_utc(when, tz)
    except ValueError:
        return
    assert result.utcoffset() == timedelta(0)
