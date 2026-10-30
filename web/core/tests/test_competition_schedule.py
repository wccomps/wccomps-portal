"""The schedule the competition timer runs from must be one it can run."""

from datetime import UTC, datetime

import pytest
from django.test import Client
from django.urls import reverse

from core.models import CompetitionConfig
from core.utils import UnknownTimezoneError, parse_datetime_to_utc

pytestmark = pytest.mark.django_db


@pytest.fixture
def admin_client(admin_user):
    client = Client()
    client.force_login(admin_user)
    return client


def _post(client, action, **data):
    return client.post(reverse("admin_competition_action"), {"action": action, **data})


def test_schedule_ending_before_it_starts_is_refused(admin_client):
    """Same date for a 16:00-00:00 UTC competition: the timer would never start it."""
    response = _post(
        admin_client,
        "set_schedule",
        start_datetime="2026-10-03T16:00",
        start_timezone="UTC",
        end_datetime="2026-10-03T00:00",
        end_timezone="UTC",
    )

    assert response.status_code == 400
    assert response.json()["error"] == "The end time must be after the start time"
    config = CompetitionConfig.get_config()
    assert (config.competition_start_time, config.competition_end_time) == (None, None)


@pytest.mark.parametrize(
    "fields",
    [
        {"start_datetime": "2026-10-04T01:00", "start_timezone": "UTC"},
        {"end_datetime": "2026-10-03T15:00", "end_timezone": "UTC"},
    ],
)
def test_one_time_is_checked_against_the_stored_other(admin_client, fields):
    CompetitionConfig.objects.update_or_create(
        pk=1,
        defaults={
            "competition_start_time": datetime(2026, 10, 3, 16, tzinfo=UTC),
            "competition_end_time": datetime(2026, 10, 4, tzinfo=UTC),
        },
    )

    response = _post(admin_client, "set_schedule", **fields)

    assert response.status_code == 400
    config = CompetitionConfig.get_config()
    assert config.competition_start_time == datetime(2026, 10, 3, 16, tzinfo=UTC)
    assert config.competition_end_time == datetime(2026, 10, 4, tzinfo=UTC)


def test_a_valid_schedule_is_saved(admin_client):
    response = _post(
        admin_client,
        "set_schedule",
        start_datetime="2026-10-03T09:00",
        start_timezone="America/Los_Angeles",
        end_datetime="2026-10-03T17:00",
        end_timezone="America/Los_Angeles",
    )

    assert response.status_code == 200
    config = CompetitionConfig.get_config()
    assert config.competition_start_time == datetime(2026, 10, 3, 16, tzinfo=UTC)
    assert config.competition_end_time == datetime(2026, 10, 4, tzinfo=UTC)


def test_unknown_timezone_is_a_400_not_a_500(admin_client):
    response = _post(admin_client, "set_schedule", start_datetime="2026-10-03T09:00", start_timezone="Mars/Olympus")

    assert response.status_code == 400
    assert response.json()["error"] == "Unknown timezone: Mars/Olympus"


def test_parse_raises_a_value_error_for_an_unknown_timezone():
    with pytest.raises(UnknownTimezoneError):
        parse_datetime_to_utc("2026-10-03T09:00", "Mars/Olympus")
    assert issubclass(UnknownTimezoneError, ValueError)


def test_setting_a_start_time_ends_the_timers_live_start_series(admin_client):
    """The timer's retries carry out the old start; the admin's new one replaces it."""
    from django.utils import timezone

    CompetitionConfig.objects.update_or_create(
        pk=1,
        defaults={"timer_series_enable": True, "timer_series_attempt": 2, "timer_series_started": timezone.now()},
    )

    response = _post(admin_client, "set_schedule", start_datetime="2030-10-03T09:00", start_timezone="UTC")

    assert response.status_code == 200
    config = CompetitionConfig.get_config()
    assert (config.timer_series_enable, config.timer_series_attempt) == (None, 0)
    assert config.competition_start_time == datetime(2030, 10, 3, 9, tzinfo=UTC)
