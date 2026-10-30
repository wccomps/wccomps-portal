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


def test_clearing_the_end_time_cancels_the_scheduled_stop(admin_client):
    from core.models import AuditLog

    start = datetime(2099, 10, 3, 16, tzinfo=UTC)
    CompetitionConfig.objects.update_or_create(
        pk=1, defaults={"competition_start_time": start, "competition_end_time": datetime(2099, 10, 4, tzinfo=UTC)}
    )

    response = _post(admin_client, "clear_end_time")

    assert response.status_code == 200, response.content
    config = CompetitionConfig.get_config()
    assert (config.competition_start_time, config.competition_end_time) == (start, None)
    assert AuditLog.objects.get(action="competition_schedule_set").details["end_time"] is None


def test_clearing_the_start_time_cancels_the_scheduled_start(admin_client):
    from core.models import AuditLog

    end = datetime(2099, 10, 4, tzinfo=UTC)
    CompetitionConfig.objects.update_or_create(
        pk=1, defaults={"competition_start_time": datetime(2099, 10, 3, 16, tzinfo=UTC), "competition_end_time": end}
    )

    response = _post(admin_client, "clear_start_time")

    assert response.status_code == 200, response.content
    config = CompetitionConfig.get_config()
    assert (config.competition_start_time, config.competition_end_time) == (None, end)
    assert AuditLog.objects.get(action="competition_schedule_set").details["start_time"] is None


def test_the_default_time_zone_is_written_once():
    from core.tests.guards import REPO_DIR, fail_with, tracked_files

    zone = "America/Los_Angeles"
    found = {
        path: "names it"
        for path in tracked_files()
        if "/tests/" not in path
        and not path.startswith("web/browser_tests/")
        and path.endswith((".py", ".html", ".js"))
        and zone in (REPO_DIR / path).read_text(encoding="utf-8")
    }
    fail_with(
        f"{zone} written outside core.utils (use DEFAULT_TIMEZONE)",
        found,
        {
            "web/core/utils.py": "TIMEZONE_CHOICES and DEFAULT_TIMEZONE",
            "web/static/js/competition.js": "formatTime shows each time in California's zone too",
        },
    )


def test_the_setup_page_defaults_to_the_default_time_zone(admin_client):
    from core.utils import DEFAULT_TIMEZONE

    page = admin_client.get(reverse("admin_competition_setup")).content.decode()

    assert f"startTimezone: '{DEFAULT_TIMEZONE}'" in page
    assert f"endTimezone: '{DEFAULT_TIMEZONE}'" in page


def test_parse_raises_a_value_error_for_an_unknown_timezone():
    with pytest.raises(UnknownTimezoneError):
        parse_datetime_to_utc("2026-10-03T09:00", "Mars/Olympus")
    assert issubclass(UnknownTimezoneError, ValueError)


@pytest.mark.parametrize("action", ["clear_start_time", "clear_end_time"])
def test_a_clear_works_on_a_stored_schedule_that_ends_before_it_starts(admin_client, action):
    """A pair that ends before it starts is fixed by clearing a time, so a clear is never refused."""
    CompetitionConfig.objects.update_or_create(
        pk=1,
        defaults={
            "competition_start_time": datetime(2099, 10, 4, tzinfo=UTC),
            "competition_end_time": datetime(2099, 10, 3, tzinfo=UTC),
        },
    )

    response = _post(admin_client, action)

    assert response.status_code == 200, response.content
    config = CompetitionConfig.get_config()
    assert (config.competition_start_time if action == "clear_start_time" else config.competition_end_time) is None
