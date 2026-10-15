"""Inputs the property tests found breaking the portal, each pinned with @example of its minimal failing input."""

from datetime import timedelta

from django.core.exceptions import ValidationError
from hypothesis import example, given
from hypothesis import strategies as st
from packets.forms import PacketResendForm
from registration.forms import RegistrationForm

from core.admin_views.competition import TIMEZONE_CHOICES
from core.forms import SchoolInfoEditForm
from core.tests.strategies import datetime_text, email_text, long_email, request_text
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


# The 254-character column every stored email goes in (models.EmailField); a form's EmailField accepts up to 320
EMAIL_COLUMN = 254
STORED_EMAILS = (
    (SchoolInfoEditForm, ("contact_email", "secondary_email")),
    (PacketResendForm, ("primary_email", "secondary_email")),
    (RegistrationForm, ("captain_email", "coach_email")),
)


@given(email=email_text())
@example(email=long_email(EMAIL_COLUMN + 1))
def test_an_email_a_form_stores_fits_its_column(email: str) -> None:
    for form_class, names in STORED_EMAILS:
        for name in names:
            try:
                cleaned = form_class.base_fields[name].clean(email)
            except ValidationError:
                continue
            assert len(cleaned) <= EMAIL_COLUMN, (
                f"{form_class.__name__}.{name} accepts a {len(cleaned)}-character email"
            )
