"""The Competition setup page: its forms save through the competition action and return to the setup page."""

from unittest.mock import MagicMock, patch

import pytest

from core.tests.roles import role_user

from .conftest import create_session_context, visit_and_capture_errors

pytestmark = [pytest.mark.browser, pytest.mark.django_db(transaction=True)]


def test_a_save_returns_to_the_setup_page_with_no_console_errors(live_server, pw_browser):
    from core.models import CompetitionConfig

    user = role_user("gold_team")
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


def _checks():
    return [
        ("Discord settings", lambda: ("pass", "All required Discord IDs are set", None)),
        (
            "Start message",
            lambda: ("warn", "No start message", {"type": "link", "url": "#start-message", "label": "Set it"}),
        ),
        (
            "Team accounts",
            lambda: ("fail", "2 accounts missing", {"type": "fix", "key": "accounts", "label": "Fix Now"}),
        ),
    ]


def test_the_readiness_results_are_a_standard_table_with_result_badges(live_server, pw_browser):
    user = role_user("gold_team")
    context = create_session_context(pw_browser, live_server, user)
    manager = MagicMock()
    manager.list_applications.return_value = []
    try:
        with (
            patch("core.admin_views.competition.AuthentikManager", return_value=manager),
            patch("core.admin_views.readiness.ALL_CHECKS", _checks()),
        ):
            page, _, errors = visit_and_capture_errors(context, f"{live_server.url}/ops/admin/competition/setup/")
            page.get_by_role("button", name="Run Checks").click()
            table = page.locator("#readiness_list")
            table.locator("tbody tr").nth(2).wait_for()

            rows = table.locator("tbody tr")
            badges = table.locator("tbody .badge")
            assert [b.inner_text().upper() for b in badges.all()] == ["PASS", "WARNING", "FAIL"]
            assert [b.get_attribute("class") for b in badges.all()] == [
                "badge badge--success",
                "badge badge--warning",
                "badge badge--danger",
            ]
            assert rows.nth(1).get_by_role("link", name="Set it").get_attribute("href") == "#start-message"
            fix = rows.nth(2).get_by_role("button", name="Fix Now")
            assert "button" in fix.get_attribute("class").split()
            assert page.locator(".btn, .btn-sm, .ms-auto").count() == 0
            assert not errors, errors
    finally:
        context.close()
