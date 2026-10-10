"""The controlled-apps editor can't lose apps when the page is stale."""

from unittest.mock import MagicMock, patch

import pytest

from .conftest import _create_role_user, create_session_context

pytestmark = [pytest.mark.browser, pytest.mark.django_db(transaction=True)]

AVAILABLE = ["containerssh", "netbird", "quotient2", "scoring"]


def test_stale_page_edit_keeps_apps_added_elsewhere(live_server, pw_browser):
    """Also covers a fresh session: the page itself must provide the CSRF token."""
    from core.models import CompetitionConfig

    config = CompetitionConfig.get_config()
    config.controlled_applications = ["scoring", "netbird"]
    config.save()

    user = _create_role_user("admin", None)
    context = create_session_context(pw_browser, live_server, user)
    page = context.new_page()
    page.on("dialog", lambda dialog: dialog.accept())
    manager = MagicMock()
    manager.list_applications.return_value = AVAILABLE

    try:
        with patch("core.admin_views.competition.AuthentikManager", return_value=manager):
            page.goto(f"{live_server.url}/ops/admin/competition/")

            # Another save lands after this page loaded (what happened mid-reload on 2026-09-27)
            config.refresh_from_db()
            config.controlled_applications = [*config.controlled_applications, "containerssh"]
            config.save()

            # Add from the stale page
            page.get_by_label("Authentik application slug").fill("quotient2")
            with page.expect_response(lambda r: r.url.endswith("/ops/admin/competition/action/")):
                page.get_by_role("button", name="Add").click()

            config.refresh_from_db()
            assert config.controlled_applications == ["scoring", "netbird", "containerssh", "quotient2"]

            # Remove from the (still stale) page
            with page.expect_response(lambda r: r.url.endswith("/ops/admin/competition/action/")):
                page.locator("button[aria-label='Remove app'][data-app=netbird]").click()

            config.refresh_from_db()
            assert config.controlled_applications == ["scoring", "containerssh", "quotient2"]
    finally:
        context.close()


def test_a_slug_missing_from_authentiks_app_list_can_be_typed_in(live_server, pw_browser):
    """Authentik lists only the apps the service account may open; a staff-only app is still addable."""
    from core.models import CompetitionConfig

    config = CompetitionConfig.get_config()
    config.controlled_applications = ["scoring"]
    config.save()

    user = _create_role_user("admin", None)
    context = create_session_context(pw_browser, live_server, user)
    page = context.new_page()
    manager = MagicMock()
    manager.list_applications.return_value = AVAILABLE  # no "competitions"
    manager.get_application_by_slug.side_effect = lambda slug: {"slug": slug} if slug == "competitions" else None

    try:
        with patch("core.admin_views.competition.AuthentikManager", return_value=manager):
            page.goto(f"{live_server.url}/ops/admin/competition/")
            page.get_by_label("Authentik application slug").fill("competitions")
            with page.expect_response(lambda r: r.url.endswith("/ops/admin/competition/action/")):
                page.get_by_role("button", name="Add").click()

            config.refresh_from_db()
            assert config.controlled_applications == ["scoring", "competitions"]
            page.locator("button[aria-label='Remove app'][data-app=competitions]").wait_for()
    finally:
        context.close()


def test_start_message_loads_and_saves(live_server, pw_browser):
    """The stored message fills the box (newlines intact) and an edit saves through the page's action."""
    from core.models import CompetitionConfig

    config = CompetitionConfig.get_config()
    config.start_message = "Default VM login\nadmin / old"
    config.save()

    user = _create_role_user("admin", None)
    context = create_session_context(pw_browser, live_server, user)
    page = context.new_page()
    manager = MagicMock()
    manager.list_applications.return_value = AVAILABLE

    try:
        with patch("core.admin_views.competition.AuthentikManager", return_value=manager):
            page.goto(f"{live_server.url}/ops/admin/competition/")
            box = page.locator("#start_message")
            assert box.input_value() == "Default VM login\nadmin / old"

            box.fill("Default VM login\nadmin / Changeme-2026!")
            with page.expect_response(lambda r: r.url.endswith("/ops/admin/competition/action/")):
                page.get_by_role("button", name="Save Start Message").click()

            config.refresh_from_db()
            assert config.start_message == "Default VM login\nadmin / Changeme-2026!"
    finally:
        context.close()
