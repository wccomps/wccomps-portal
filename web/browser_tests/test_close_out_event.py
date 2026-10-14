"""Close Out Event on the competition page: warnings, the typed confirmation, and the streamed steps."""

from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from .conftest import _create_role_user, create_session_context

pytestmark = [pytest.mark.browser, pytest.mark.django_db(transaction=True)]


def _open_event_with_a_score():
    from registration.models import Event, Season
    from registration.services import set_team_school
    from scoring.models import InjectScore, ServiceScore

    from team.models import Team

    season = Season.objects.create(name="2026", year=2026, is_active=True)
    event = Event.objects.create(
        season=season, name="Invitational", event_type="invitational", date=date(2026, 10, 3), is_active=True
    )
    team, _ = Team.objects.get_or_create(team_number=1, defaults={"team_name": "Team 01", "is_active": True})
    set_team_school(team, "Example University", contact_email="coach@example.edu")
    ServiceScore.objects.create(team=team, service_points=Decimal(100))
    InjectScore.objects.create(team=team, inject_id="1", inject_name="Memo", points_awarded=Decimal(5))
    return event


@pytest.mark.parametrize("typed", ["nope", "yolo"])
def test_close_out_needs_the_typed_word_then_streams_to_done(live_server, pw_browser, typed):
    from core.models import DiscordTask

    event = _open_event_with_a_score()
    user = _create_role_user("admin", None)
    context = create_session_context(pw_browser, live_server, user)
    page = context.new_page()
    page.on("dialog", lambda dialog: dialog.accept(typed))
    manager = MagicMock()
    manager.list_applications.return_value = []

    try:
        with patch("core.admin_views.competition.AuthentikManager", return_value=manager):
            page.goto(f"{live_server.url}/ops/admin/competition/")
            # The check loads after the page, with a link to where each item is finished
            page.get_by_role("link", name="1 inject grade is not approved.").wait_for()
            assert page.get_by_role("link", name="Scorecards haven't been emailed for this event.").is_visible()

            if typed != "yolo":
                page.get_by_role("button", name="Close Out Event").click()
                page.wait_for_timeout(500)
                event.refresh_from_db()
                assert event.is_active
                assert not DiscordTask.objects.exists()
                return

            with page.expect_response(lambda r: r.url.endswith("/ops/admin/competition/action/")):
                page.get_by_role("button", name="Close Out Event").click()
            page.get_by_text("Closed out Invitational:").wait_for()

        event.refresh_from_db()
        assert not event.is_active and event.is_finalized
        assert DiscordTask.objects.filter(task_type="cleanup_competition").count() == 1
    finally:
        context.close()


def test_a_failing_check_leaves_close_out_usable(live_server, pw_browser):
    _open_event_with_a_score()
    user = _create_role_user("admin", None)
    context = create_session_context(pw_browser, live_server, user)
    page = context.new_page()
    manager = MagicMock()
    manager.list_applications.return_value = []

    try:
        with (
            patch("core.admin_views.competition.AuthentikManager", return_value=manager),
            patch("core.admin_views.competition.close_out_warnings", side_effect=RuntimeError("scoring broke")),
        ):
            page.goto(f"{live_server.url}/ops/admin/competition/")
            page.get_by_text("Couldn't check unfinished work.").wait_for()
            assert page.get_by_role("button", name="Close Out Event").is_enabled()
            assert page.get_by_role("button", name="Start Competition").is_enabled()
    finally:
        context.close()


def test_cancel_event_on_the_danger_page(live_server, pw_browser):
    from registration.models import Event, Season
    from registration.services import set_team_school

    from team.models import Team

    season = Season.objects.create(name="2026", year=2026, is_active=True)
    event = Event.objects.create(
        season=season, name="Invitational", event_type="invitational", date=date(2026, 10, 3), is_active=True
    )
    team, _ = Team.objects.get_or_create(team_number=1, defaults={"team_name": "Team 01", "is_active": True})
    set_team_school(team, "Example University", contact_email="coach@example.edu")
    user = _create_role_user("admin", None)
    context = create_session_context(pw_browser, live_server, user)
    page = context.new_page()
    page.on("dialog", lambda dialog: dialog.accept("yolo"))

    try:
        page.goto(f"{live_server.url}/ops/admin/competition/danger/")
        with page.expect_response(lambda r: r.url.endswith("/ops/admin/competition/action/")):
            page.get_by_text("Cancel Event", exact=True).last.click()
        page.get_by_text("Invitational cancelled. The school list stays.").wait_for()

        event.refresh_from_db()
        assert not event.is_active
        assert not event.team_assignments.exists()
    finally:
        context.close()
