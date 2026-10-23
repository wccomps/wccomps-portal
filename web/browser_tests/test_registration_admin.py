"""The registration admin pages (seasons and events) render, link to their forms, and confirm before deleting."""

from datetime import date

import pytest

from core.tests.roles import role_user

from .conftest import create_session_context, visit_and_capture_errors

pytestmark = [pytest.mark.browser, pytest.mark.django_db(transaction=True)]


def test_seasons_and_events_pages(live_server, pw_browser):
    from registration.models import Event, Season

    viewer = role_user("gold_team")
    season = Season.objects.create(name="Example Season", year=2026, is_active=True)
    event = Event.objects.create(season=season, name="Invitational", event_type="invitational", date=date(2026, 11, 7))

    context = create_session_context(pw_browser, live_server, viewer)
    try:
        page, status, errors = visit_and_capture_errors(context, f"{live_server.url}/register/seasons/")
        assert status == 200
        assert page.get_by_role("link", name="Create Season").first.is_visible()
        assert page.get_by_role("button", name="Edit", exact=True).is_visible()
        page.get_by_role("button", name="Events", exact=True).click()
        page.wait_for_load_state("networkidle")
        assert page.get_by_role("cell", name="Invitational").first.is_visible()
        assert page.get_by_role("link", name="Create Event").first.is_visible()
        assert page.get_by_role("button", name="Edit", exact=True).is_visible()

        page.get_by_role("button", name="View", exact=True).click()
        page.wait_for_load_state("networkidle")
        assert page.get_by_role("heading", name="Team Enrollments (0)").is_visible()
        assert page.get_by_text("No teams enrolled for this event").is_visible()

        page.go_back()
        page.wait_for_load_state("networkidle")
        page.get_by_role("button", name="Delete", exact=True).click()
        page.wait_for_load_state("networkidle")
        assert page.get_by_text(f'Are you sure you want to delete the event "{event.name}"?').is_visible()
        assert page.get_by_role("button", name="Delete Event").is_visible()
        page.get_by_role("button", name="Cancel", exact=True).click()
        page.wait_for_load_state("networkidle")
        assert Event.objects.filter(pk=event.pk).exists()
        assert not errors, errors
    finally:
        context.close()
