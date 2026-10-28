import re
from collections.abc import Callable
from datetime import datetime, timedelta

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from orange_team.forms import MAX_MINUTES
from orange_team.models import MAX_POINTS, OrangeCheck

pytestmark = pytest.mark.django_db


@pytest.fixture
def lead_client(create_user_with_groups: Callable[..., User]) -> Client:
    c = Client()
    c.force_login(create_user_with_groups("orangelead", ["WCComps_OrangeTeam_Lead"]))
    return c


def test_create_check_with_max_and_window_no_criteria(lead_client: Client) -> None:
    resp = lead_client.post(
        "/orange-team/checks/create/",
        {"title": "Firewall Change", "description": "do it", "max_points": "25", "time_limit_minutes": "60"},
    )
    assert resp.status_code == 302
    check = OrangeCheck.objects.get(title="Firewall Change")
    assert check.max_points == 25
    assert check.time_limit == timedelta(minutes=60)
    assert check.max_score == 25  # no criteria -> falls back to max_points


def test_create_check_blank_window_is_null(lead_client: Client) -> None:
    lead_client.post("/orange-team/checks/create/", {"title": "No window", "description": "d", "max_points": "10"})
    check = OrangeCheck.objects.get(title="No window")
    assert check.time_limit is None


def test_edit_check_updates_max_and_window(lead_client: Client) -> None:
    check = OrangeCheck.objects.create(title="C", description="d", max_points=5)
    resp = lead_client.post(
        f"/orange-team/checks/{check.id}/edit/",
        {"title": "C", "description": "d", "max_points": "30", "time_limit_minutes": "15"},
    )
    assert resp.status_code == 302
    check.refresh_from_db()
    assert check.max_points == 30
    assert check.time_limit == timedelta(minutes=15)


def _field_error(html: str, field: str) -> str | None:
    """The error shown in a field's own row, under the id its widget's aria-describedby names."""
    match = re.search(rf'<p class="errornote" id="id_{field}_error">([^<]*)</p>', html)
    return match.group(1) if match else None


def test_a_refused_create_shows_the_posted_values_and_the_error_beside_its_field(lead_client: Client) -> None:
    response = lead_client.post(
        "/orange-team/checks/create/",
        {
            "title": "Keeps its title",
            "description": "Posted steps",
            "scheduled_at": "2026-10-30T09:15",
            "time_limit_minutes": "45",
            "max_points": str(MAX_POINTS + 1),
        },
    )
    html = response.content.decode()

    assert response.status_code == 200
    assert not OrangeCheck.objects.filter(title="Keeps its title").exists()
    assert _field_error(html, "max_points") == f"Ensure this value is less than or equal to {MAX_POINTS}."
    assert _field_error(html, "title") is None
    assert 'value="Keeps its title"' in html
    assert "Posted steps</textarea>" in html
    assert 'value="2026-10-30T09:15"' in html
    assert 'value="45"' in html


def test_the_edit_page_shows_the_saved_check_in_its_inputs_and_the_field_limits(lead_client: Client) -> None:
    check = OrangeCheck.objects.create(
        title="C",
        description="d",
        max_points=5,
        time_limit=timedelta(hours=2),
        scheduled_at=datetime(2026, 10, 30, 9, 15, tzinfo=timezone.get_current_timezone()),
    )
    html = lead_client.get(f"/orange-team/checks/{check.id}/edit/").content.decode()

    assert re.search(r'type="datetime-local" name="scheduled_at" value="2026-10-30T09:15"', html)
    assert re.search(rf'name="time_limit_minutes" value="120"[^>]* max="{MAX_MINUTES}"', html)
    assert re.search(rf'name="max_points" value="5"[^>]* max="{MAX_POINTS}"', html)
