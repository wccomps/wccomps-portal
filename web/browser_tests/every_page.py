"""Every page the portal answers a GET with, for checks that must hold on all of them (contrast, phone width).

The pages come from the view property rows (core.tests.view_rows), which test_property_coverage keeps to one per
URL pattern, so a new page is walked without being listed here. Each is rendered as the first staff role it lets
in that gets it, and as a blue team and a signed-out visitor where they get it: those see a different nav and page.
test_every_page_renders fails on a page no role gets, unless UNRENDERED says why."""

from collections.abc import Iterator
from contextlib import ExitStack
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

import pytest
from django.contrib.auth.models import User
from django.urls import reverse
from playwright.sync_api import Browser, BrowserContext, Page, ViewportSize
from pytest_django.live_server_helper import LiveServer

from core.errors import is_json_endpoint
from core.tests.view_rows import VIEW_MODULES
from core.tests.views_harness import CALLBACKS, ViewCase, World, build_world, client_for, offline, route_params

from .conftest import create_session_context

STAFF_ORDER = [
    "admin",
    "gold_team",
    "white_team_lead",
    "white_team",
    "orange_team_lead",
    "orange_team",
    "red_team",
    "ticketing_admin",
    "ticketing_support",
    "no_role",
]

CSV = "A CSV download (JSON with ?format=json)"
FILE = "A download: the stored file, served as its type"
PDF = "A download: the team's scorecard as a PDF"
ZIP = "A download: a zip of "

# URL name -> why no role gets a page from it to check
UNRENDERED: dict[str, str] = {
    "download_packet": FILE,
    "home": "Redirects each role to its landing page",
    "leaderboard_scorecard_pdf": PDF,
    "link_initiate": "Starts the sign-in for a Discord link token (400 without one); its error page is link_callback's",
    "livez": "The liveness probe: plain text",
    "oauth_login": "Redirects to Authentik's sign-in",
    "ops_ticket_detail_redirect": "The old ops address of a ticket: a permanent redirect to its page",
    "ops_ticket_list_redirect": "The old ops address of the ticket list: a permanent redirect to it",
    "orange_team:assignment_screenshot": FILE,
    "orange_team:export_scores": "A CSV download",
    "school_info_export": "A CSV download",
    "scoring:export_all": ZIP + "every export",
    "scoring:export_final_scores": CSV,
    "scoring:export_incidents": CSV,
    "scoring:export_inject_grades": CSV,
    "scoring:export_orange_adjustments": CSV,
    "scoring:export_red_scores": CSV,
    "scoring:export_scorecards": ZIP + "every team's scorecard PDF",
    "scoring:export_tickets": CSV,
    "scoring:incident_screenshot": FILE,
    "scoring:orange_team_redirect": "The old Orange Team address: a redirect to its dashboard",
    "scoring:red_screenshot": FILE,
    "scoring:scorecard_pdf": PDF,
    "scoring:submit_orange_check": "The old Orange Team address: a redirect to its dashboard",
    "ticket_attachment_download": FILE,
    "ticket_box_reset_panel": "A Box Reset ticket's htmx panel, and the world's first ticket isn't one (404)",
}


@dataclass(frozen=True)
class Visit:
    name: str  # the URL name
    role: str
    path: str  # with its query string
    user: User | None  # the role's user; None signed out


def _shown(name: str) -> bool:
    """Django's own admin pages are left out but for its changelists and index: a model's add, change and delete
    forms are Django's, its changelist draws the columns this repo's admin classes define."""
    return not name.startswith("admin:") or name.endswith("_changelist") or name == "admin:index"


def pages(world: World) -> Iterator[tuple[ViewCase, str, str]]:
    """Each URL name a row drives that may answer a GET with a page, with its row and its path: the world's first
    row for each path parameter, and the row's entry query. A row's patches (Quotient's injects) stay on while the
    caller works with its page, so iterate to the end or close the iterator."""
    for module in VIEW_MODULES:
        for case in module.CASES:
            for name in (case.name, *case.also):
                # A sign-out GET ends the world's sessions; a JSON endpoint and a POST-only view are no page
                if "logout" in name or "GET" not in case.methods or is_json_endpoint(CALLBACKS[name]):
                    continue
                if not _shown(name):
                    continue
                kwargs = {param: world.ids(kind)[0] for _, param, kind in route_params(case, name)}
                path = reverse(name, kwargs=kwargs)
                query = case.entry_query(world) if case.entry_query else {}
                if query:
                    path += "?" + "&".join(f"{key}={value}" for key, values in query.items() for value in values)
                with case.patches(world) if case.patches else ExitStack():
                    yield case, name, path


def _renders(world: World, role: str, path: str) -> bool:
    response = client_for(world, role).get(path)
    return response.status_code == 200 and not response.streaming and "text/html" in response.get("Content-Type", "")


def rendered_as(world: World, case: ViewCase, path: str) -> list[str]:
    """The roles a page is rendered as, each one the row lets in that gets 200 with HTML: the first staff role that
    does (a red teamer's pool is a 404 to admin), the blue team and a signed-out visitor."""
    staff = next((role for role in STAFF_ORDER if role in case.allowed and _renders(world, role, path)), None)
    others = [role for role in ("blue_team", "anonymous") if role in case.allowed and _renders(world, role, path)]
    return [staff, *others] if staff else others


def open_pages(
    pw_browser: Browser,
    live_server: LiveServer,
    viewport: ViewportSize,
    schemes: tuple[Literal["light", "dark"], ...] = ("light",),
) -> Iterator[tuple[Visit, str, Page]]:
    """Each visit loaded in the browser at the viewport, once per color scheme, in a context per role and scheme: the
    visit, the scheme and the page, which closes when the caller moves on. A page that answers an error or lands
    elsewhere (the sign-in) fails rather than being checked. Iterate under contextlib.closing, so the contexts close
    when a check fails."""
    contexts: dict[tuple[str, str], BrowserContext] = {}
    try:
        with offline():
            world = build_world()
            for case, name, path in pages(world):
                for role in rendered_as(world, case, path):
                    visit = Visit(name, role, path, world.users.get(role))
                    for scheme in schemes:
                        if (visit.role, scheme) not in contexts:
                            contexts[visit.role, scheme] = create_session_context(
                                pw_browser, live_server, visit.user, viewport=viewport, color_scheme=scheme
                            )
                        page = contexts[visit.role, scheme].new_page()
                        try:
                            response = page.goto(live_server.url + visit.path, wait_until="networkidle")
                            where = f"{visit.name} as {visit.role} ({scheme})"
                            if response is None or not response.ok:
                                pytest.fail(f"{where} answered {response and response.status}", pytrace=False)
                            if urlsplit(page.url).path != urlsplit(visit.path).path:
                                pytest.fail(f"{where} landed on {page.url}, not {visit.path}", pytrace=False)
                            yield visit, scheme, page
                        finally:
                            page.close()
    finally:
        for context in contexts.values():
            context.close()
