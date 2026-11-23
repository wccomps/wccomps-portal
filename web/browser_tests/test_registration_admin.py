"""The registration admin pages (seasons and events) render, link to their forms, and confirm before deleting."""

from datetime import date

import pytest
from django.urls import reverse

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
        assert page.get_by_role("heading", name="Teams", exact=True).is_visible()
        assert page.get_by_text("No teams for this event").is_visible()

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


def test_django_admin_edits_the_events_proxy_team_domain(live_server, pw_browser, current_event):
    with create_session_context(pw_browser, live_server, role_user("admin")) as context:
        page = context.new_page()
        path = reverse("admin:registration_event_change", args=[current_event.pk])
        assert page.goto(f"{live_server.url}{path}").status == 200
        domain = page.get_by_label("Proxy team domain")
        domain.fill("boxx.event.example..")
        with page.expect_navigation():
            page.get_by_role("button", name="Save", exact=True).click()
        assert page.get_by_text("Enter a valid team DNS domain.", exact=True).is_visible()

        domain.fill("boxx.event.example")
        with page.expect_navigation():
            page.get_by_role("button", name="Save", exact=True).click()
        current_event.refresh_from_db()
        assert current_event.proxy_team_domain == "boxx.event.example"
