"""Browser test fixtures: Playwright + session injection, no OAuth required."""

from collections.abc import Iterator
from typing import TYPE_CHECKING

import pytest
from django.test import Client
from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright

if TYPE_CHECKING:
    from django.contrib.auth.models import User
    from pytest_django.live_server_helper import LiveServer


@pytest.fixture(scope="session")
def _allow_sync_orm_under_playwright() -> Iterator[None]:
    """Playwright's sync API runs an event loop on the main thread, which trips Django's async-safety check.

    Switched on only when a browser launches (pw_browser requests it): as an autouse fixture any
    test in this directory, browser or not, turned the check off for the rest of the worker's
    session, hiding sync ORM calls on the bot's event loop.
    """
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("DJANGO_ALLOW_ASYNC_UNSAFE", "true")
        yield


@pytest.fixture(scope="session")
def pw_browser(_allow_sync_orm_under_playwright: None) -> Iterator[Browser]:
    """Launch a single Chromium browser instance for the whole test session."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        yield browser
        browser.close()


def create_session_context(
    pw_browser: Browser,
    live_server: LiveServer,
    user: User | None,
) -> BrowserContext:
    """Create a Playwright BrowserContext with the user's session cookie injected."""
    context = pw_browser.new_context(viewport={"width": 1920, "height": 1080})

    if user is not None:
        # force_login creates a real session in the test DB
        client = Client()
        client.force_login(user)
        session_cookie = client.cookies["sessionid"]
        context.add_cookies(
            [
                {
                    "name": "sessionid",
                    "value": session_cookie.value,
                    "url": live_server.url,
                }
            ]
        )

    return context


def visit_and_capture_errors(
    context: BrowserContext,
    url: str,
) -> tuple[Page, int, list[str]]:
    """Navigate to a URL, capture console errors and page errors.

    Returns (page, status_code, errors).
    """
    page = context.new_page()
    errors: list[str] = []

    def _on_console(msg):
        if msg.type == "error":
            errors.append(f"[console.error] {msg.text}")

    page.on("console", _on_console)
    page.on("pageerror", lambda err: errors.append(f"[pageerror] {err}"))

    response = page.goto(url, wait_until="networkidle")
    status_code = response.status if response else 0

    return page, status_code, errors


def make_test_data():
    """Create shared test data needed by pages with needs_data."""
    from datetime import timedelta
    from decimal import Decimal

    from django.utils import timezone
    from orange_team.models import OrangeCheck
    from packets.models import Packet
    from scoring.models import IncidentReport, RedTeamScore

    from team.models import Team
    from ticketing.models import Ticket

    team, _ = Team.objects.get_or_create(
        team_number=1,
        defaults={
            "team_name": "Test Team 01",
            "authentik_group": "WCComps_BlueTeam01",
            "is_active": True,
        },
    )

    ticket = Ticket.objects.create(
        ticket_number="TEAM01-001",
        title="Browser Test Ticket",
        description="Created for browser tests",
        team=team,
        status="open",
    )

    finding = RedTeamScore.objects.create(
        source_ip="10.0.0.1", points_per_team=Decimal("10")
    )
    finding.affected_teams.add(team)
    incident = IncidentReport.objects.create(
        team=team,
        attack_description="Saw an SSH login",
        source_ip="10.0.0.1",
        attack_detected_at=timezone.now() - timedelta(minutes=30),
    )
    orange_check = OrangeCheck.objects.create(title="Browser Test Check", description="Created for browser tests")
    packet = Packet.objects.create(
        title="Browser Test Packet",
        file_data=b"%PDF-1.4",
        filename="packet.pdf",
        mime_type="application/pdf",
        file_size=8,
        uploaded_by="browser-test",
    )

    return {
        "ticket": ticket,
        "team": team,
        "orange_check": orange_check,
        "packet": packet,
        "finding": finding,
        "incident": incident,
    }


def resolve_url(page_def, test_data: dict) -> str:
    """Build the URL path for a PageDef, filling dynamic kwargs from test_data."""
    from django.urls import reverse

    kwargs = dict(page_def.url_kwargs)
    if page_def.needs_data == "ticket":
        kwargs["ticket_number"] = test_data["ticket"].ticket_number
    elif page_def.needs_data == "team":
        kwargs["team_number"] = test_data["team"].team_number
    elif page_def.needs_data == "orange_check":
        kwargs["check_id"] = test_data["orange_check"].pk
    elif page_def.needs_data == "packet":
        kwargs["packet_id"] = test_data["packet"].pk
    elif page_def.needs_data == "finding":
        kwargs["finding_id"] = test_data["finding"].pk
    elif page_def.needs_data == "incident":
        kwargs["incident_id"] = test_data["incident"].pk
    return reverse(page_def.url_name, kwargs=kwargs)
