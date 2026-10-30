"""Close Out Event on the competition page: warnings, the typed confirmation, and the streamed steps; and the
rare actions under Advanced."""

from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from core.tests.roles import role_user

from .conftest import create_session_context

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
    user = role_user("admin")
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
            assert page.get_by_role(
                "link",
                name="No scorecard email is recorded for this event. If you already emailed them, you can close out.",
            ).is_visible()

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
        assert DiscordTask.objects.filter(task_type="reconcile_teams").count() == 1
    finally:
        context.close()


def test_a_failing_check_leaves_close_out_usable(live_server, pw_browser):
    _open_event_with_a_score()
    user = role_user("admin")
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
            page.get_by_role("alert").get_by_text("Couldn't check unfinished work.").wait_for()
            assert "Couldn't check unfinished work." in page.locator("#close-out").inner_text()
            assert page.get_by_role("button", name="Close Out Event").is_enabled()
            assert page.get_by_role("button", name="Start Competition").is_enabled()
    finally:
        context.close()


def test_cancel_event_under_advanced(live_server, pw_browser):
    from registration.models import Event, Season
    from registration.services import set_team_school

    from team.models import Team

    season = Season.objects.create(name="2026", year=2026, is_active=True)
    event = Event.objects.create(
        season=season, name="Invitational", event_type="invitational", date=date(2026, 10, 3), is_active=True
    )
    team, _ = Team.objects.get_or_create(team_number=1, defaults={"team_name": "Team 01", "is_active": True})
    set_team_school(team, "Example University", contact_email="coach@example.edu")
    user = role_user("admin")
    context = create_session_context(pw_browser, live_server, user)
    page = context.new_page()
    page.on("dialog", lambda dialog: dialog.accept("yolo"))

    try:
        page.goto(f"{live_server.url}/ops/admin/competition/")
        cancel = page.get_by_role("button", name="Cancel Event", exact=True)
        assert not cancel.is_visible()
        page.get_by_role("button", name="Advanced: reset team passwords, cancel the event").click()
        with page.expect_response(lambda r: r.url.endswith("/ops/admin/competition/action/")):
            cancel.click()
        page.get_by_text("Invitational cancelled. The school list stays.").wait_for()

        event.refresh_from_db()
        assert not event.is_active
        assert not event.team_assignments.exists()
    finally:
        context.close()


@pytest.mark.parametrize("typed", ["nope", "yolo"])
def test_clear_leftover_data_opens_under_advanced_and_needs_the_typed_word(live_server, pw_browser, typed):
    from team.models import Team
    from ticketing.models import Ticket

    team = Team.objects.create(team_number=1, team_name="Team 01", is_active=True)
    Ticket.objects.create(ticket_number="T001-001", team=team, title="Left")
    user = role_user("admin")
    context = create_session_context(pw_browser, live_server, user)
    page = context.new_page()
    page.on("dialog", lambda dialog: dialog.accept(typed))

    try:
        page.goto(f"{live_server.url}/ops/admin/competition/")
        # The Leftover data section points at Advanced, which is open while Clear leftover data is next
        clear = page.get_by_role("button", name="Clear Leftover Data", exact=True)
        clear.wait_for()
        if typed == "yolo":
            with page.expect_response(lambda r: r.url.endswith("/ops/admin/competition/action/")):
                clear.click()
            page.get_by_text("Leftover data cleared.").wait_for()
            assert not Ticket.objects.exists()
        else:
            clear.click()
            page.wait_for_timeout(500)
            assert Ticket.objects.exists()
    finally:
        context.close()


@pytest.mark.parametrize("on_load", [True, False])
def test_a_link_to_advanced_opens_it(live_server, pw_browser, on_load):
    user = role_user("admin")
    context = create_session_context(pw_browser, live_server, user)
    page = context.new_page()
    toggle = page.get_by_role("button", name="Advanced: reset team passwords")

    try:
        if on_load:
            page.goto(f"{live_server.url}/ops/admin/competition/#advanced")
        else:
            page.goto(f"{live_server.url}/ops/admin/competition/")
            toggle.wait_for()
            assert toggle.get_attribute("aria-expanded") == "false"
            page.evaluate("location.hash = 'advanced'")
        page.get_by_role("button", name="Reset Passwords").wait_for()
        assert toggle.get_attribute("aria-expanded") == "true"
    finally:
        context.close()


def test_a_blocking_problem_comes_first_and_the_warnings_never_say_go_ahead(live_server, pw_browser):
    from team.models import Team

    _open_event_with_a_score()
    Team.objects.filter(team_number=1).update(is_active=False)
    user = role_user("admin")
    context = create_session_context(pw_browser, live_server, user)
    page = context.new_page()
    manager = MagicMock()
    manager.list_applications.return_value = []

    try:
        with patch("core.admin_views.competition.AuthentikManager", return_value=manager):
            page.goto(f"{live_server.url}/ops/admin/competition/")
            section = page.locator("#close-out")
            section.get_by_role("link", name="1 inject grade is not approved.").wait_for()
            text = section.inner_text()
            assert "Team 1 is inactive" in text
            assert text.index("Team 1 is inactive") < text.index("1 inject grade is not approved.")
            assert "Also unfinished" in text
            assert "don't block" not in text
            assert page.get_by_role("button", name="Close Out Event").is_disabled()
    finally:
        context.close()
