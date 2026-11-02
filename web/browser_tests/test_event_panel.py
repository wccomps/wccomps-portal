"""The competition page in three stages: no event open, running, and an event played and stopped."""

import re
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from core.tests.roles import role_user

from .conftest import create_session_context, visit_and_capture_errors

pytestmark = [pytest.mark.browser, pytest.mark.django_db(transaction=True)]


def _played_event():
    from registration.models import Event, Season
    from registration.services import set_team_school
    from scoring.models import ServiceScore

    from team.models import Team

    season = Season.objects.create(name="WCComps 2027", year=2027, is_active=True)
    Event.objects.create(
        season=season, name="Invitational 1", event_type="invitational", date=date(2027, 10, 2), is_active=True
    )
    team, _ = Team.objects.get_or_create(team_number=1, defaults={"team_name": "Team 01", "is_active": True})
    set_team_school(team, "Example University", contact_email="coach@example.edu")
    ServiceScore.objects.create(team=team, service_points=Decimal(100))


def _controls(page):
    """How many visible buttons and links on the page are named for starting, stopping or closing out."""
    names = page.locator("[x-data=competitionAdmin]").locator("button:visible, a:visible").all_inner_texts()
    return {
        action: sum(1 for name in names if re.match(action, name.strip(), re.IGNORECASE))
        for action in ("start", "stop", "close out")
    }


def test_with_no_event_the_event_section_sends_you_to_the_import(live_server, pw_browser):
    user = role_user("gold_team")
    context = create_session_context(pw_browser, live_server, user)
    manager = MagicMock()
    manager.__enter__.return_value = manager
    manager.list_applications.return_value = []
    try:
        with patch("core.admin_views.competition.AuthentikManager", return_value=manager):
            page, status, errors = visit_and_capture_errors(context, f"{live_server.url}/ops/admin/competition/")
            panel = page.locator("#event-panel")
            text = panel.inner_text()
            action = panel.locator("a.button").first
            assert status == 200
            assert "No event open" in text
            assert "Import the next school list to open an event." in text
            assert (action.inner_text(), action.get_attribute("href")) == (
                "Import the school list",
                "/ops/school-info/import/",
            )
            assert _controls(page) == {"start": 1, "stop": 1, "close out": 0}
            assert page.locator("#controls").get_by_role("button", name="Start Competition").is_enabled()
            assert page.locator("#controls").get_by_role("button", name="Stop Competition").is_disabled()
            assert not errors, errors
    finally:
        context.close()


def test_played_and_stopped_the_page_checks_unfinished_work(live_server, pw_browser):
    _played_event()
    user = role_user("gold_team")
    context = create_session_context(pw_browser, live_server, user)
    manager = MagicMock()
    manager.__enter__.return_value = manager
    manager.list_applications.return_value = []
    try:
        with patch("core.admin_views.competition.AuthentikManager", return_value=manager):
            page, status, errors = visit_and_capture_errors(context, f"{live_server.url}/ops/admin/competition/")
            panel = page.locator("#event-panel")
            # The close-out check loads after the page and lists the unfinished work in the section below
            page.locator("#close-out").get_by_role("link", name="No scorecard email is recorded").wait_for()
            text = panel.inner_text()
            action = panel.locator("a.button").first
            assert status == 200
            assert "Invitational 1" in text and "Season\nWCComps 2027" in text
            assert "STOPPED" in text.upper()
            assert [s.inner_text().upper() for s in page.locator("#steps_list .badge").all()] == [
                "DONE",
                "LATER",
                "DONE",
                "DONE",
                "NOW",
                "NEXT",
            ]
            assert page.locator("#readiness").count() == 0 and page.locator("#live").count() == 0
            assert _controls(page) == {"start": 1, "stop": 1, "close out": 1}
            assert page.locator("#controls").get_by_role("button", name="Start Competition").is_enabled()
            assert page.locator("#controls").get_by_role("button", name="Stop Competition").is_disabled()
            assert page.locator("#steps_list td a", has_text="Close Out").count() == 0
            assert (action.inner_text(), action.get_attribute("href")) == (
                "Email scorecards",
                "/scoring/email-scorecards/",
            )
            assert not errors, errors
    finally:
        context.close()


def test_while_running_the_page_offers_stop_and_shows_the_live_numbers(live_server, pw_browser):
    from core.models import CompetitionConfig

    _played_event()
    CompetitionConfig.objects.update_or_create(pk=1, defaults={"applications_enabled": True})
    user = role_user("gold_team")
    context = create_session_context(pw_browser, live_server, user)
    try:
        page, status, errors = visit_and_capture_errors(context, f"{live_server.url}/ops/admin/competition/")
        panel = page.locator("#event-panel")
        assert status == 200
        assert "RUNNING" in panel.inner_text().upper()
        # Stop is in Competition Controls only; Start there is disabled, since no start has left anything off
        assert panel.get_by_role("button", name="Stop Competition").count() == 0
        assert page.locator("#controls").get_by_role("button", name="Start Competition").is_disabled()
        assert page.locator("#controls").get_by_role("button", name="Stop Competition").is_enabled()
        assert _controls(page) == {"start": 1, "stop": 1, "close out": 0}
        live = page.locator("#live")
        assert "Open or claimed tickets" in live.inner_text()
        assert "Never synced" in live.inner_text()
        assert page.locator("#close-out").count() == 0
        assert not page.get_by_role("button", name="Reset Passwords").is_visible()
        assert not errors, errors
    finally:
        context.close()
