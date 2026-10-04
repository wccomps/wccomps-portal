"""Controlled-app edits apply one slug to the current list, so a stale page can't drop others."""

from unittest.mock import patch

import pytest
from django.test import Client
from django.urls import reverse

from core.models import AuditLog, CompetitionConfig

pytestmark = pytest.mark.django_db


@pytest.fixture
def admin_client(admin_user):
    client = Client()
    client.force_login(admin_user)
    return client


@pytest.fixture(autouse=True)
def authentik_apps():
    """Slugs Authentik knows; get_application_by_slug finds these whatever its app list shows."""
    known = {"scoring", "netbird", "containerssh", "quotient2", "competitions"}
    with patch(
        "core.admin_views.competition.AuthentikManager.get_application_by_slug",
        side_effect=lambda slug: {"slug": slug} if slug in known else None,
    ) as lookup:
        yield lookup


@pytest.fixture
def config():
    config = CompetitionConfig.get_config()
    config.controlled_applications = ["scoring", "netbird"]
    config.save()
    return config


def _post(client, action, **data):
    return client.post(reverse("admin_competition_action"), {"action": action, **data})


def test_add_app_appends_to_current_list(admin_client, config):
    response = _post(admin_client, "add_app", app_slug="containerssh")

    assert response.status_code == 200
    assert response.json()["apps"] == ["scoring", "netbird", "containerssh"]
    config.refresh_from_db()
    assert config.controlled_applications == ["scoring", "netbird", "containerssh"]


def test_edit_from_stale_page_keeps_apps_added_since(admin_client, config):
    """Reproduces the lost update: page A loaded before containerssh was added elsewhere."""
    _post(admin_client, "add_app", app_slug="containerssh")  # e.g. from another tab / during reload

    _post(admin_client, "add_app", app_slug="quotient2")  # stale page only knows scoring, netbird

    config.refresh_from_db()
    assert config.controlled_applications == ["scoring", "netbird", "containerssh", "quotient2"]


def test_add_app_normalizes_and_ignores_duplicates(admin_client, config):
    _post(admin_client, "add_app", app_slug="  NetBird ")

    config.refresh_from_db()
    assert config.controlled_applications == ["scoring", "netbird"]


def test_remove_app_removes_only_that_app(admin_client, config):
    response = _post(admin_client, "remove_app", app_slug="scoring")

    assert response.status_code == 200
    assert response.json()["apps"] == ["netbird"]
    config.refresh_from_db()
    assert config.controlled_applications == ["netbird"]


def test_remove_missing_app_is_harmless(admin_client, config):
    response = _post(admin_client, "remove_app", app_slug="jumpbox")

    assert response.status_code == 200
    config.refresh_from_db()
    assert config.controlled_applications == ["scoring", "netbird"]


def test_edits_are_audited_with_full_resulting_list(admin_client, config):
    _post(admin_client, "add_app", app_slug="containerssh")

    entry = AuditLog.objects.filter(action="competition_apps_configured").latest("pk")
    assert entry.details["controlled_apps"] == ["scoring", "netbird", "containerssh"]
    assert entry.details["added"] == "containerssh"


def test_blank_slug_rejected(admin_client, config):
    response = _post(admin_client, "add_app", app_slug="   ")

    assert response.status_code == 400
    config.refresh_from_db()
    assert config.controlled_applications == ["scoring", "netbird"]


def test_blue_team_cannot_edit_apps(blue_team_user, config):
    client = Client()
    client.force_login(blue_team_user)

    response = _post(client, "add_app", app_slug="containerssh")

    assert response.status_code == 403
    config.refresh_from_db()
    assert config.controlled_applications == ["scoring", "netbird"]


def test_page_sets_csrf_cookie_for_fresh_session(admin_user):
    """wcPost reads the csrftoken cookie; the page must set it even with no prior form visit."""
    from unittest.mock import patch

    client = Client(enforce_csrf_checks=True)
    client.force_login(admin_user)

    with patch("core.admin_views.competition.AuthentikManager") as manager:
        manager.return_value.list_applications.return_value = []
        response = client.get(reverse("admin_competition"))

    assert response.status_code == 200
    assert "csrftoken" in response.cookies
    assert 'name="csrf-token"' in response.content.decode()


def test_app_list_is_fetched_separately_from_the_page(admin_client):
    """Authentik's app list takes ~1s, so the page renders without it and the picker fetches it."""
    from unittest.mock import patch

    with patch("core.admin_views.competition.AuthentikManager") as manager:
        manager.return_value.list_applications.return_value = ["netbird", "scoring"]
        page = admin_client.get(reverse("admin_competition"))
        manager.return_value.list_applications.assert_not_called()
        apps = admin_client.get(reverse("admin_competition_apps"))

    assert page.status_code == 200
    assert apps.json() == {"apps": ["netbird", "scoring"]}


def test_app_list_denied_without_admin_or_gold(blue_team_user):
    client = Client()
    client.force_login(blue_team_user)
    response = client.get(reverse("admin_competition_apps"))
    assert response.status_code == 403


def test_setting_the_schedule_leaves_an_emptied_app_list_empty(admin_client, config):
    """Removing every app is a choice: setting times must not refill the list from Authentik."""
    from unittest.mock import patch

    _post(admin_client, "remove_app", app_slug="scoring")
    _post(admin_client, "remove_app", app_slug="netbird")

    with patch("core.authentik_manager.AuthentikManager") as manager:
        for action in ("set_start_time", "set_end_time"):
            _post(admin_client, action, datetime="2026-10-03T09:00", timezone="America/Los_Angeles")
        _post(admin_client, "set_schedule", start_datetime="2026-10-03T09:00", end_datetime="2026-10-03T17:00")

    manager.assert_not_called()
    config.refresh_from_db()
    assert config.controlled_applications == []
    assert config.competition_end_time is not None


def test_add_app_rejects_a_slug_authentik_does_not_have(admin_client, config):
    response = _post(admin_client, "add_app", app_slug="competitons")

    assert response.status_code == 400
    assert "competitons" in response.json()["error"]
    config.refresh_from_db()
    assert config.controlled_applications == ["scoring", "netbird"]


def test_remove_app_does_not_need_authentik(admin_client, config, authentik_apps):
    _post(admin_client, "remove_app", app_slug="netbird")

    authentik_apps.assert_not_called()
