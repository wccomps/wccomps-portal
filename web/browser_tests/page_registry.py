"""Central registry of pages the browser tests render, and element expectations.

Each page is rendered as every browser role its view property row lets in (core.tests.views_harness), less the
roles it skips with a reason; which roles a page refuses is checked there for every URL pattern."""

from dataclasses import dataclass, field
from functools import cache

# The roles the role-wide browser tests visit as
BROWSER_ROLES = [
    "blue_team",
    "red_team",
    "gold_team",
    "orange_team",
    "white_team",
    "ticketing_support",
    "ticketing_admin",
    "admin",
]


@cache
def _view_rows_allowed() -> dict[str, frozenset[str]]:
    from core.tests.test_property_coverage import VIEW_MODULES

    return {case.name: case.allowed for module in VIEW_MODULES for case in module.CASES}


@dataclass
class PageDef:
    url_name: str
    url_kwargs: dict[str, str | int] = field(default_factory=dict)
    needs_data: str | None = None  # "ticket", "team", "orange_check", "packet" — keys into test_data dict
    # A browser role the view row lets in that the page isn't rendered as -> why
    skip_roles: dict[str, str] = field(default_factory=dict)
    expect_redirect: bool = False  # True if page redirects (e.g., home)
    checks: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    # checks format: {"role": {"present": ["css selectors"], "absent": ["css selectors"]}}

    @property
    def allowed_roles(self) -> list[str]:
        """The browser roles its view row lets in, less the skipped ones."""
        allowed = _view_rows_allowed()[self.url_name]
        return [role for role in BROWSER_ROLES if role in allowed and role not in self.skip_roles]


# Pages to test. Add new entries here to expand coverage.
PAGES: list[PageDef] = [
    # =========================================================================
    # Ticketing
    # =========================================================================
    PageDef(
        url_name="ticket_list",
        checks={
            "blue_team": {"present": ["table"], "absent": [".bulk-claim-form"]},
            "admin": {"present": ["table"]},
        },
    ),
    PageDef(
        url_name="create_ticket",
        skip_roles={
            "gold_team": "the same staff form admin is rendered with",
            "ticketing_admin": "the same staff form admin is rendered with",
        },
        checks={
            "blue_team": {"present": ["form", "select[name='category']"]},
            "admin": {"present": ["form"]},
        },
    ),
    PageDef(
        url_name="ticket_detail",
        skip_roles={"ticketing_admin": "the same staff page ticketing_support is rendered with"},
        needs_data="ticket",
    ),
    PageDef(
        url_name="ops_review_tickets",
    ),
    # =========================================================================
    # Admin / Ops
    # =========================================================================
    PageDef(
        url_name="admin_competition",
    ),
    PageDef(
        url_name="admin_competition_setup",
    ),
    PageDef(
        url_name="admin_competition_guide",
    ),
    PageDef(
        url_name="admin_teams",
    ),
    PageDef(
        url_name="admin_team_detail",
        needs_data="team",
    ),
    PageDef(
        url_name="admin_broadcast",
    ),
    PageDef(
        url_name="admin_sync_roles",
    ),
    PageDef(
        url_name="admin_categories",
    ),
    PageDef(
        url_name="admin_category_create",
    ),
    # =========================================================================
    # School Info / Core Ops
    # =========================================================================
    PageDef(
        url_name="school_info",
    ),
    # =========================================================================
    # Scoring — Leaderboard & Config
    # =========================================================================
    PageDef(
        url_name="leaderboard_page",
    ),
    PageDef(
        url_name="scoring:scoring_config",
    ),
    # =========================================================================
    # Scoring — Red Team
    # =========================================================================
    PageDef(
        url_name="scoring:submit_red_score",
    ),
    PageDef(
        url_name="scoring:red_team_scores",
    ),
    PageDef(
        url_name="scoring:red_team_findings",
    ),
    PageDef(
        url_name="scoring:ip_pool_list",
    ),
    PageDef(
        url_name="scoring:ip_pool_create",
    ),
    # =========================================================================
    # Scoring — Incidents (Blue Team)
    # =========================================================================
    # scoring:submit_incident_report removed — depends on Quotient API
    PageDef(
        url_name="scoring:incident_list",
    ),
    PageDef(
        url_name="scoring:review_incidents",
    ),
    # =========================================================================
    # Scoring — Orange Team
    # =========================================================================
    PageDef(
        url_name="scoring:review_orange",
    ),
    # =========================================================================
    # Scoring — Injects (White Team)
    # =========================================================================
    # scoring:inject_grading removed — depends on Quotient API
    PageDef(
        url_name="scoring:inject_grades_review",
    ),
    PageDef(
        url_name="scoring:review_inject_feedback",
    ),
    # =========================================================================
    # Scoring — Export & Email
    # =========================================================================
    PageDef(
        url_name="scoring:export_index",
    ),
    PageDef(
        url_name="scoring:email_scorecards",
    ),
    PageDef(
        url_name="scoring:scorecard",
        needs_data="team",
    ),
    # =========================================================================
    # Orange Team
    # =========================================================================
    PageDef(
        url_name="orange_team:dashboard",
    ),
    PageDef(
        url_name="orange_team:check_list",
    ),
    PageDef(
        url_name="orange_team:check_create",
    ),
    PageDef(
        url_name="orange_team:review_queue",
    ),
    PageDef(
        url_name="orange_team:check_detail",
        needs_data="orange_check",
    ),
    # =========================================================================
    # Packets
    # =========================================================================
    PageDef(
        url_name="team_packet",
        skip_roles={"gold_team": "the same every-packet list admin is rendered with"},
    ),
    PageDef(
        url_name="packets_list",
    ),
    PageDef(
        url_name="packet_detail",
        needs_data="packet",
    ),
    PageDef(
        url_name="upload_packet",
    ),
    # =========================================================================
    # Home (redirects based on role)
    # =========================================================================
    PageDef(
        url_name="home",
        skip_roles={
            "orange_team": "home only redirects to a role's landing page, rendered on that page's own row",
            "ticketing_support": "home only redirects to a role's landing page, rendered on that page's own row",
            "ticketing_admin": "home only redirects to a role's landing page, rendered on that page's own row",
        },
        expect_redirect=True,
    ),
]


def get_allowed_test_cases() -> list[tuple[PageDef, str]]:
    """Generate (page_def, role) pairs for pages each role CAN access."""
    return [(page, role) for page in PAGES for role in page.allowed_roles]


def get_element_check_cases() -> list[tuple[PageDef, str]]:
    """Generate (page_def, role) pairs that have element checks defined."""
    return [(page, role) for page in PAGES for role in page.checks]
