"""Central registry of pages the browser tests render, and element expectations.

Each page is rendered as every browser role its view property row lets in (core.tests.view_rows), less the
roles it skips with a reason; which roles a page refuses is checked there for every URL pattern."""

from dataclasses import dataclass, field

from core.tests.roles import BROWSER_ROLES


@dataclass
class PageDef:
    url_name: str
    url_kwargs: dict[str, str | int] = field(default_factory=dict)
    needs_data: str | None = None  # a key of conftest.make_test_data's dict: the row the URL names
    # A browser role the view row lets in that the page isn't rendered as -> why
    skip_roles: dict[str, str] = field(default_factory=dict)
    expect_redirect: bool = False  # True if page redirects (e.g., home)
    checks: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    # checks format: {"role": {"present": ["css selectors"], "absent": ["css selectors"]}}

    @property
    def allowed_roles(self) -> list[str]:
        """The browser roles its view row lets in, less the skipped ones."""
        from core.tests.view_rows import allowed_roles

        allowed = allowed_roles(self.url_name)
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
        skip_roles={"ticketing_admin": "the same staff page admin is rendered with"},
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
        url_name="scoring:view_red_score",
        needs_data="finding",
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
    PageDef(
        url_name="scoring:view_incident_report",
        needs_data="incident",
    ),
    PageDef(
        url_name="scoring:match_incident",
        needs_data="incident",
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
        # Home only redirects to a role's landing page: one visit per landing page, except that blue_team and admin
        # both land on the ticket list (its team and staff views)
        skip_roles={
            "white_team": "lands on the leaderboard, as gold_team does",
            "ticketing_support": "lands on the ticket list, as blue_team and admin do",
            "ticketing_admin": "lands on the ticket list, as blue_team and admin do",
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
