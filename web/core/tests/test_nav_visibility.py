"""Test that primary nav and scoring sub-nav visibility matches role permissions.

Renders real pages via the Django test client and parses the HTML to check
which nav links each role actually sees. If someone changes a template
{% if %} condition or a permission mapping, these tests catch the mismatch.

Source templates:
  - templates/admin/base_site.html  (primary nav)
  - templates/scoring/base.html     (scoring sub-nav)
"""

import re

import pytest
from django.test import Client

from core.tests.roles import BROWSER_ROLES, role_user

pytestmark = pytest.mark.django_db


# A page each browser role (core.tests.roles.BROWSER_ROLES) can open, which renders the primary nav.
# Different roles need different pages since no single page is accessible to all.
ROLE_TEST_URL: dict[str, str] = {
    "blue_team": "/scoring/incident/list/",
    "red_team": "/scoring/red-team/",
    "gold_team": "/leaderboard/",
    "orange_team": "/orange-team/",
    "white_team": "/scoring/incidents/",
    "ticketing_support": "/tickets/",
    "ticketing_admin": "/tickets/",
    "admin": "/tickets/",
}

# Page that renders scoring/base.html (has the scoring sub-nav).
SCORING_TEST_URL = "/scoring/incidents/"

# ---------------------------------------------------------------------------
# Expected nav links per role (the single source of truth for what's correct)
# ---------------------------------------------------------------------------
EXPECTED_PRIMARY_NAV: dict[str, set[str]] = {
    "blue_team": {"Incident Report", "Packets"},
    "red_team": {"Red Team Findings", "Leaderboard"},
    "gold_team": {
        "Incident Report",
        "Packets",
        "Orange Team",
        "Leaderboard",
        "Scoring Review",
        "White Team",
        "Competition",
    },
    "orange_team": {"Orange Team", "Leaderboard"},
    "white_team": {"Incident Report", "Leaderboard", "Scoring Review", "White Team"},
    "ticketing_support": {"Tickets", "Leaderboard"},
    "ticketing_admin": {"Tickets", "Leaderboard"},
    "admin": {
        "Tickets",
        "Incident Report",
        "Packets",
        "Red Team Findings",
        "Orange Team",
        "Leaderboard",
        "Scoring Review",
        "White Team",
        "Competition",
        "Django Admin",
    },
}

# For each role the scoring review page lets in
EXPECTED_SCORING_SUBNAV: dict[str, set[str]] = {
    "gold_team": {"Red Team Scores", "Orange Checks", "Incidents", "Inject Grades", "Ticket Points"},
    "white_team": {"Incidents", "Inject Grades"},
    "admin": {"Red Team Scores", "Orange Checks", "Incidents", "Inject Grades", "Ticket Points"},
}


def _extract_primary_nav_links(html: str) -> set[str]:
    """Extract link texts from the primary nav (.nav-links div)."""
    match = re.search(r'<div class="nav-links">(.*?)</div>', html, re.DOTALL)
    if not match:
        return set()
    nav_html = match.group(1)
    links = set()
    for a_match in re.finditer(r"<a[^>]*>(.*?)</a>", nav_html, re.DOTALL):
        # Strip child HTML tags (e.g. notification badge <span>)
        text = re.sub(r"<[^>]+>", "", a_match.group(1)).strip()
        if text:
            links.add(text)
    return links


def _extract_scoring_subnav_links(html: str) -> set[str]:
    """Extract link texts from the scoring sub-nav (<nav role="navigation">)."""
    match = re.search(r'<nav[^>]*role="navigation"[^>]*>(.*?)</nav>', html, re.DOTALL)
    if not match:
        return set()
    nav_html = match.group(1)
    links = set()
    for a_match in re.finditer(r'<a[^>]*class="nav-item[^"]*"[^>]*>(.*?)</a>', nav_html, re.DOTALL):
        text = re.sub(r"<[^>]+>", "", a_match.group(1)).strip()
        if text:
            links.add(text)
    return links


class TestRenderedPrimaryNav:
    """Render real pages and verify each role sees exactly the correct primary nav links."""

    @pytest.mark.parametrize("role", BROWSER_ROLES)
    def test_primary_nav_links(self, role):
        user = role_user(role)
        client = Client()
        client.force_login(user)

        response = client.get(ROLE_TEST_URL[role])
        assert response.status_code == 200, f"{role} got {response.status_code} on {ROLE_TEST_URL[role]}"

        html = response.content.decode()
        visible = _extract_primary_nav_links(html)
        expected = EXPECTED_PRIMARY_NAV[role]
        assert visible == expected, (
            f"{role}: primary nav mismatch on {ROLE_TEST_URL[role]}\n"
            f"  Expected: {sorted(expected)}\n"
            f"  Got:      {sorted(visible)}\n"
            f"  Extra:    {sorted(visible - expected)}\n"
            f"  Missing:  {sorted(expected - visible)}"
        )


class TestRenderedScoringSubnav:
    """Render the scoring review page and verify each role sees the correct sub-nav."""

    @pytest.mark.parametrize("role", EXPECTED_SCORING_SUBNAV)
    def test_scoring_subnav_links(self, role):
        user = role_user(role)
        client = Client()
        client.force_login(user)

        response = client.get(SCORING_TEST_URL)
        assert response.status_code == 200, f"{role} got {response.status_code} on {SCORING_TEST_URL}"

        html = response.content.decode()
        visible = _extract_scoring_subnav_links(html)
        expected = EXPECTED_SCORING_SUBNAV[role]
        assert visible == expected, (
            f"{role}: scoring subnav mismatch\n"
            f"  Expected: {sorted(expected)}\n"
            f"  Got:      {sorted(visible)}\n"
            f"  Extra:    {sorted(visible - expected)}\n"
            f"  Missing:  {sorted(expected - visible)}"
        )


def test_the_tables_cover_the_roles_their_pages_let_in() -> None:
    """Every browser role has a page and expected links, and the scoring sub-nav is checked for exactly the roles the
    scoring review page lets in."""
    from core.tests.view_rows import allowed_roles

    assert set(ROLE_TEST_URL) == set(EXPECTED_PRIMARY_NAV) == set(BROWSER_ROLES)
    scoring = allowed_roles("scoring:review_incidents") & set(BROWSER_ROLES)
    assert set(EXPECTED_SCORING_SUBNAV) == scoring
