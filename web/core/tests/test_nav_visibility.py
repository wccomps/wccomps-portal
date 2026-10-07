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
from django.contrib.auth.models import User
from django.test import Client

from core.models import UserGroups
from team.models import DiscordLink, Team

pytestmark = pytest.mark.django_db

# Role name → Authentik group(s)
ROLE_GROUPS: dict[str, list[str]] = {
    "blue_team": ["WCComps_BlueTeam01"],
    "red_team": ["WCComps_RedTeam"],
    "gold_team": ["WCComps_GoldTeam"],
    "orange_team": ["WCComps_OrangeTeam"],
    "white_team": ["WCComps_WhiteTeam"],
    "ticketing_support": ["WCComps_Ticketing_Support"],
    "ticketing_admin": ["WCComps_Ticketing_Admin"],
    "admin": ["WCComps_Discord_Admin"],
}

# Page each role can access (must return 200 and render the primary nav).
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

# Roles that can access the scoring review page.
SCORING_ROLES = ["gold_team", "white_team", "admin"]

# ---------------------------------------------------------------------------
# Expected nav links per role (the single source of truth for what's correct)
# ---------------------------------------------------------------------------
EXPECTED_PRIMARY_NAV: dict[str, set[str]] = {
    "blue_team": {"Incident Report"},
    "red_team": {"Red Team Findings", "Leaderboard"},
    "gold_team": {"Incident Report", "Orange Team", "Leaderboard", "Scoring Review", "White Team", "Competition"},
    "orange_team": {"Orange Team", "Leaderboard"},
    "white_team": {"Incident Report", "Leaderboard", "Scoring Review", "White Team"},
    "ticketing_support": {"Tickets", "Leaderboard"},
    "ticketing_admin": {"Tickets", "Leaderboard"},
    "admin": {
        "Tickets",
        "Incident Report",
        "Red Team Findings",
        "Orange Team",
        "Leaderboard",
        "Scoring Review",
        "White Team",
        "Competition",
        "Django Admin",
    },
}

EXPECTED_SCORING_SUBNAV: dict[str, set[str]] = {
    "gold_team": {"Red Team Scores", "Orange Checks", "Incidents", "Inject Grades", "Ticket Points"},
    "white_team": {"Incidents", "Inject Grades"},
    "admin": {"Red Team Scores", "Orange Checks", "Incidents", "Inject Grades", "Ticket Points"},
}


def _create_role_user(role: str) -> User:
    """Create a test user with the given role's Authentik groups + required links."""
    groups = ROLE_GROUPS[role]
    username = f"navrender_{role}"
    user = User.objects.create_user(username=username, password="testpass123")
    UserGroups.objects.create(user=user, authentik_id=f"{username}-uid", groups=groups)

    team = None
    if role == "blue_team":
        team, _ = Team.objects.get_or_create(
            team_number=1,
            defaults={"team_name": "Test Team 01", "authentik_group": "WCComps_BlueTeam01", "is_active": True},
        )

    DiscordLink.objects.create(
        user=user,
        discord_id=8000000 + list(ROLE_GROUPS.keys()).index(role),
        discord_username=username,
        team=team,
        is_active=True,
    )
    return user


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

    @pytest.mark.parametrize("role", ROLE_GROUPS.keys())
    def test_primary_nav_links(self, role):
        user = _create_role_user(role)
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

    @pytest.mark.parametrize("role", SCORING_ROLES)
    def test_scoring_subnav_links(self, role):
        user = _create_role_user(role)
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
