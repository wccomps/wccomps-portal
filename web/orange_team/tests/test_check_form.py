from collections.abc import Callable
from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.test import Client

from orange_team.models import OrangeCheck

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
