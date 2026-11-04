"""Times are stored in UTC and shown and entered in the viewer's own timezone (the tz cookie)."""

import zoneinfo
from datetime import UTC, date, datetime

import pytest
from django.test import Client
from django.utils import timezone
from orange_team.models import OrangeCheck
from registration.forms import EventForm
from registration.models import Event, Season

pytestmark = pytest.mark.django_db


def _create_check(user, cookie: str | None) -> OrangeCheck:
    client = Client()
    client.force_login(user)
    if cookie is not None:
        client.cookies["tz"] = cookie
    client.post(
        "/orange-team/checks/create/",
        {
            "title": "Phones",
            "scheduled_at": "2026-10-03T09:00",
            "criterion_label_0": "Polite",
            "criterion_points_0": "5",
        },
    )
    return OrangeCheck.objects.get(title="Phones")


@pytest.mark.parametrize(
    ("cookie", "stored_utc_hour"),
    [
        ("America/Los_Angeles", 16),
        ("America%2FLos_Angeles", 16),
        (None, 9),
        ("Not/AZone", 9),
        ("Not%2FAZone", 9),
    ],
)
def test_datetime_input_is_read_in_the_viewers_timezone(gold_team_user, cookie, stored_utc_hour):
    check = _create_check(gold_team_user, cookie)
    assert check.scheduled_at == datetime(2026, 10, 3, stored_utc_hour, 0, tzinfo=UTC)


def test_encoded_timezone_cookie_renders_page_and_csv_timestamps_in_local_time(gold_team_user, blue_team):
    from orange_team.models import OrangeAssignment

    check = OrangeCheck.objects.create(
        title="Router Audit",
        description="Verify firewall rules",
        scheduled_at=datetime(2026, 10, 3, 16, 30, tzinfo=UTC),
        created_by=gold_team_user,
    )
    OrangeAssignment.objects.create(
        orange_check=check,
        team=blue_team,
        user=gold_team_user,
        status="approved",
        submitted_at=datetime(2026, 10, 3, 17, 45, tzinfo=UTC),
    )

    client = Client()
    client.force_login(gold_team_user)
    client.cookies["tz"] = "America%2FLos_Angeles"

    detail_response = client.get(f"/orange-team/checks/{check.pk}/")
    assert detail_response.status_code == 200
    assert "Oct 03, 2026 09:30" in detail_response.content.decode()

    csv_response = client.get("/orange-team/export/")
    assert csv_response.status_code == 200
    assert "2026-10-03 10:45" in csv_response.content.decode()


def test_datetime_input_is_prefilled_in_the_viewers_timezone(gold_team_user):
    event = Event(
        season=Season(name="2026", year=2026),
        name="Qualifier",
        date=date(2026, 10, 3),
        registration_deadline=datetime(2026, 10, 1, 7, 0, tzinfo=UTC),
    )
    with timezone.override(zoneinfo.ZoneInfo("America/Los_Angeles")):
        html = str(EventForm(instance=event)["registration_deadline"])

    assert 'value="2026-10-01T00:00"' in html
