"""The Event panel on the competition page, in two stages: no event open, and an event played and stopped."""

from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from .conftest import _create_role_user, create_session_context, visit_and_capture_errors

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


def test_with_no_event_the_panel_sends_you_to_the_import(live_server, pw_browser):
    user = _create_role_user("gold_team", None)
    context = create_session_context(pw_browser, live_server, user)
    manager = MagicMock()
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
            assert not errors, errors
    finally:
        context.close()


def test_played_and_stopped_the_panel_checks_unfinished_work(live_server, pw_browser):
    _played_event()
    user = _create_role_user("gold_team", None)
    context = create_session_context(pw_browser, live_server, user)
    manager = MagicMock()
    manager.list_applications.return_value = []
    try:
        with patch("core.admin_views.competition.AuthentikManager", return_value=manager):
            page, status, errors = visit_and_capture_errors(context, f"{live_server.url}/ops/admin/competition/")
            panel = page.locator("#event-panel")
            # The close-out check loads after the page and fills in the step's line
            panel.get_by_text("to finish: see Close Out Event below.").wait_for()
            text = panel.inner_text()
            action = panel.locator("a.button").first
            assert status == 200
            assert "Invitational 1, season WCComps 2027" in text
            assert "Invitational 1 (WCComps 2027) is open and teams have scores." in text
            assert (action.inner_text(), action.get_attribute("href")) == (
                "Email scorecards",
                "/scoring/email-scorecards/",
            )
            assert not errors, errors
    finally:
        context.close()
