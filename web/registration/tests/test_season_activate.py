"""Season.activate, the only writer of Season.is_active, takes the school list lock and leaves one active season."""

import pytest
from django.contrib.auth.models import User
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from core.models import UserGroups
from registration.models import Season

pytestmark = pytest.mark.django_db


@pytest.fixture
def gold_client(gold_team_user):
    client = Client()
    client.force_login(gold_team_user)
    return client


def test_activate_takes_the_lock_and_leaves_one_active_season():
    old = Season.objects.create(name="2025", year=2025, is_active=True)
    new = Season.objects.create(name="2026", year=2026)

    with CaptureQueriesContext(connection) as queries:
        new.activate()

    sql = [q["sql"] for q in queries.captured_queries]
    lock = next(i for i, q in enumerate(sql) if "pg_advisory_xact_lock" in q)
    write = next(i for i, q in enumerate(sql) if q.startswith('UPDATE "registration_season"'))
    assert lock < write
    old.refresh_from_db()
    new.refresh_from_db()
    assert (old.is_active, new.is_active) == (False, True)
    assert Season.objects.active() == new


def test_creating_an_active_season_on_the_season_page_deactivates_the_others(gold_client):
    old = Season.objects.create(name="2025", year=2025, is_active=True)

    gold_client.post(reverse("registration_season_create"), {"name": "2026", "year": 2026, "is_active": "on"})

    assert list(Season.objects.filter(is_active=True).values_list("year", flat=True)) == [2026]
    old.refresh_from_db()
    assert not old.is_active


def test_activating_on_the_edit_page_deactivates_the_others(gold_client):
    Season.objects.create(name="2025", year=2025, is_active=True)
    other = Season.objects.create(name="2026", year=2026)

    gold_client.post(
        reverse("registration_season_edit", args=[other.pk]), {"name": "2026", "year": 2026, "is_active": "on"}
    )

    assert list(Season.objects.filter(is_active=True)) == [other]


def test_the_edit_page_cannot_deactivate_the_active_season(gold_client):
    season = Season.objects.create(name="2026", year=2026, is_active=True)

    gold_client.post(reverse("registration_season_edit", args=[season.pk]), {"name": "Renamed", "year": 2026})

    season.refresh_from_db()
    assert (season.name, season.is_active) == ("Renamed", True)


def test_the_admin_shows_is_active_read_only_and_activates_through_its_action():
    admin = User.objects.create(username="admin", is_staff=True, is_superuser=True)
    UserGroups.objects.update_or_create(user=admin, defaults={"groups": ["WCComps_Discord_Admin"], "authentik_id": "x"})
    client = Client()
    client.force_login(admin)
    old = Season.objects.create(name="2025", year=2025, is_active=True)
    new = Season.objects.create(name="2026", year=2026)

    page = client.get(reverse("admin:registration_season_change", args=[new.pk])).content.decode()
    client.post(
        reverse("admin:registration_season_changelist"),
        {"action": "activate_season", "_selected_action": [new.pk]},
    )

    assert 'name="is_active"' not in page
    old.refresh_from_db()
    assert not old.is_active
    assert Season.objects.active() == new
