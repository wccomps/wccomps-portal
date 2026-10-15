"""The Competition setup page: its forms save through the competition action and return to the setup page."""

from unittest.mock import MagicMock, patch

import pytest

from .conftest import _create_role_user, create_session_context, visit_and_capture_errors

pytestmark = [pytest.mark.browser, pytest.mark.django_db(transaction=True)]


def test_a_save_returns_to_the_setup_page_with_no_console_errors(live_server, pw_browser):
    from core.models import CompetitionConfig

    user = _create_role_user("gold_team", None)
    context = create_session_context(pw_browser, live_server, user)
    manager = MagicMock()
    manager.list_applications.return_value = ["scoring"]
    url = f"{live_server.url}/ops/admin/competition/setup/"
    try:
        with patch("core.admin_views.competition.AuthentikManager", return_value=manager):
            page, status, errors = visit_and_capture_errors(context, url)
            assert status == 200
            for heading in ("Schedule", "Start message", "Team access", "Scoring engine (Quotient)", "Readiness check"):
                assert page.get_by_role("heading", name=heading).is_visible(), heading

            page.locator("#max_members").fill("6")
            with page.expect_navigation(url=url, timeout=10000):
                page.get_by_role("button", name="Update").click()

            assert CompetitionConfig.get_config().max_team_members == 6
            assert page.locator("#max_members").input_value() == "6"
            assert page.locator(".breadcrumbs").get_by_role("link", name="Competition").get_attribute("href") == (
                "/ops/admin/competition/"
            )
            assert not errors, errors
    finally:
        context.close()
