"""Central registry of pages to test, with role access and element expectations."""

from dataclasses import dataclass, field


@dataclass
class PageDef:
    url_name: str
    url_kwargs: dict[str, str | int] = field(default_factory=dict)
    needs_data: str | None = None  # "ticket", "team", "orange_check", "packet" — keys into test_data dict
    allowed_roles: list[str] = field(default_factory=list)
    denied_roles: list[str] = field(default_factory=list)
    expect_redirect: bool = False  # True if page redirects (e.g., home)
    checks: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    # checks format: {"role": {"present": ["css selectors"], "absent": ["css selectors"]}}


# Pages to test. Add new entries here to expand coverage.
PAGES: list[PageDef] = [
    # =========================================================================
    # Ticketing
    # =========================================================================
    PageDef(
        url_name="ticket_list",
        allowed_roles=["blue_team", "ticketing_support", "ticketing_admin", "admin"],
        denied_roles=["red_team", "gold_team", "orange_team", "unauthenticated"],
        checks={
            "blue_team": {"present": ["table"], "absent": [".bulk-claim-form"]},
            "admin": {"present": ["table"]},
        },
    ),
    PageDef(
        url_name="create_ticket",
        allowed_roles=["blue_team", "admin"],
        denied_roles=["red_team", "unauthenticated"],
        checks={
            "blue_team": {"present": ["form", "select[name='category']"]},
            "admin": {"present": ["form"]},
        },
    ),
    PageDef(
        url_name="ticket_detail",
        needs_data="ticket",
        allowed_roles=["blue_team", "ticketing_support", "admin"],
        denied_roles=["unauthenticated"],
    ),
    PageDef(
        url_name="ops_review_tickets",
        allowed_roles=["ticketing_admin", "gold_team", "admin"],
        denied_roles=["blue_team", "red_team", "ticketing_support", "unauthenticated"],
    ),
    # =========================================================================
    # Admin / Ops
    # =========================================================================
    PageDef(
        url_name="admin_competition",
        allowed_roles=["gold_team", "admin"],
        denied_roles=["blue_team", "red_team", "ticketing_support", "unauthenticated"],
    ),
    PageDef(
        url_name="admin_teams",
        allowed_roles=["gold_team", "admin"],
        denied_roles=["blue_team", "red_team", "unauthenticated"],
    ),
    PageDef(
        url_name="admin_team_detail",
        needs_data="team",
        allowed_roles=["gold_team", "admin"],
        denied_roles=["blue_team", "unauthenticated"],
    ),
    PageDef(
        url_name="admin_broadcast",
        allowed_roles=["gold_team", "admin"],
        denied_roles=["blue_team", "red_team", "ticketing_support", "unauthenticated"],
    ),
    PageDef(
        url_name="admin_sync_roles",
        allowed_roles=["gold_team", "admin"],
        denied_roles=["blue_team", "red_team", "ticketing_support", "unauthenticated"],
    ),
    PageDef(
        url_name="admin_categories",
        allowed_roles=["gold_team", "ticketing_admin", "admin"],
        denied_roles=["blue_team", "red_team", "ticketing_support", "unauthenticated"],
    ),
    PageDef(
        url_name="admin_category_create",
        allowed_roles=["gold_team", "ticketing_admin", "admin"],
        denied_roles=["blue_team", "red_team", "ticketing_support", "unauthenticated"],
    ),
    # =========================================================================
    # School Info / Core Ops
    # =========================================================================
    PageDef(
        url_name="school_info",
        allowed_roles=["gold_team", "admin"],
        denied_roles=["blue_team", "red_team", "ticketing_support", "unauthenticated"],
    ),
    # =========================================================================
    # Scoring — Leaderboard & Config
    # =========================================================================
    PageDef(
        url_name="leaderboard_page",
        allowed_roles=[
            "gold_team",
            "white_team",
            "red_team",
            "ticketing_admin",
            "orange_team",
            "ticketing_support",
            "admin",
        ],
        denied_roles=["blue_team", "unauthenticated"],
    ),
    PageDef(
        url_name="scoring:scoring_config",
        allowed_roles=["gold_team", "admin"],
        denied_roles=["blue_team", "red_team", "unauthenticated"],
    ),
    # =========================================================================
    # Scoring — Red Team
    # =========================================================================
    PageDef(
        url_name="scoring:submit_red_score",
        allowed_roles=["red_team", "admin"],
        denied_roles=["blue_team", "unauthenticated"],
    ),
    PageDef(
        url_name="scoring:red_team_scores",
        allowed_roles=["red_team", "gold_team", "admin"],
        denied_roles=["blue_team", "unauthenticated"],
    ),
    PageDef(
        url_name="scoring:red_team_findings",
        allowed_roles=["red_team", "gold_team", "admin"],
        denied_roles=["blue_team", "unauthenticated"],
    ),
    PageDef(
        url_name="scoring:ip_pool_list",
        allowed_roles=["red_team", "admin"],
        denied_roles=["blue_team", "gold_team", "unauthenticated"],
    ),
    PageDef(
        url_name="scoring:ip_pool_create",
        allowed_roles=["red_team", "admin"],
        denied_roles=["blue_team", "gold_team", "unauthenticated"],
    ),
    # =========================================================================
    # Scoring — Incidents (Blue Team)
    # =========================================================================
    # scoring:submit_incident_report removed — depends on Quotient API
    PageDef(
        url_name="scoring:incident_list",
        allowed_roles=["blue_team", "white_team", "gold_team", "admin"],
        denied_roles=["unauthenticated"],
    ),
    PageDef(
        url_name="scoring:review_incidents",
        allowed_roles=["gold_team", "white_team", "admin"],
        denied_roles=["blue_team", "red_team", "unauthenticated"],
    ),
    # =========================================================================
    # Scoring — Orange Team
    # =========================================================================
    PageDef(
        url_name="scoring:review_orange",
        allowed_roles=["orange_team", "gold_team", "admin"],
        denied_roles=["blue_team", "red_team", "unauthenticated"],
    ),
    # =========================================================================
    # Scoring — Injects (White Team)
    # =========================================================================
    # scoring:inject_grading removed — depends on Quotient API
    PageDef(
        url_name="scoring:inject_grades_review",
        allowed_roles=["white_team", "gold_team", "admin"],
        denied_roles=["blue_team", "red_team", "unauthenticated"],
    ),
    PageDef(
        url_name="scoring:review_inject_feedback",
        allowed_roles=["gold_team", "white_team", "admin"],
        denied_roles=["blue_team", "red_team", "unauthenticated"],
    ),
    # =========================================================================
    # Scoring — Export & Email
    # =========================================================================
    PageDef(
        url_name="scoring:export_index",
        allowed_roles=["gold_team", "admin"],
        denied_roles=["blue_team", "red_team", "unauthenticated"],
    ),
    PageDef(
        url_name="scoring:email_scorecards",
        allowed_roles=["gold_team", "admin"],
        denied_roles=["blue_team", "red_team", "unauthenticated"],
    ),
    PageDef(
        url_name="scoring:scorecard",
        needs_data="team",
        allowed_roles=[
            "gold_team",
            "white_team",
            "red_team",
            "ticketing_admin",
            "orange_team",
            "ticketing_support",
            "admin",
        ],
        denied_roles=["blue_team", "unauthenticated"],
    ),
    # =========================================================================
    # Orange Team
    # =========================================================================
    PageDef(
        url_name="orange_team:dashboard",
        allowed_roles=["orange_team", "gold_team", "admin"],
        denied_roles=["blue_team", "red_team", "unauthenticated"],
    ),
    PageDef(
        url_name="orange_team:check_list",
        allowed_roles=["gold_team", "admin"],
        denied_roles=["blue_team", "red_team", "orange_team", "unauthenticated"],
    ),
    PageDef(
        url_name="orange_team:check_create",
        allowed_roles=["gold_team", "admin"],
        denied_roles=["blue_team", "red_team", "orange_team", "unauthenticated"],
    ),
    PageDef(
        url_name="orange_team:review_queue",
        allowed_roles=["gold_team", "admin"],
        denied_roles=["blue_team", "orange_team", "unauthenticated"],
    ),
    PageDef(
        url_name="orange_team:check_detail",
        needs_data="orange_check",
        allowed_roles=["gold_team", "admin"],
        denied_roles=["blue_team", "orange_team", "unauthenticated"],
    ),
    # =========================================================================
    # Packets
    # =========================================================================
    PageDef(
        url_name="team_packet",
        allowed_roles=["blue_team", "admin"],
        denied_roles=["red_team", "unauthenticated"],
    ),
    PageDef(
        url_name="packets_list",
        allowed_roles=["gold_team", "admin"],
        denied_roles=["blue_team", "red_team", "unauthenticated"],
    ),
    PageDef(
        url_name="packet_detail",
        needs_data="packet",
        allowed_roles=["gold_team", "admin"],
        denied_roles=["blue_team", "red_team", "unauthenticated"],
    ),
    PageDef(
        url_name="upload_packet",
        allowed_roles=["gold_team", "admin"],
        denied_roles=["blue_team", "red_team", "unauthenticated"],
    ),
    # =========================================================================
    # Home (redirects based on role)
    # =========================================================================
    PageDef(
        url_name="home",
        expect_redirect=True,
        allowed_roles=["blue_team", "red_team", "gold_team", "white_team", "admin"],
        denied_roles=[],
    ),
]


def get_allowed_test_cases() -> list[tuple[PageDef, str]]:
    """Generate (page_def, role) pairs for pages each role CAN access."""
    return [(page, role) for page in PAGES for role in page.allowed_roles]


def get_denied_test_cases() -> list[tuple[PageDef, str]]:
    """Generate (page_def, role) pairs for pages each role should be DENIED."""
    return [(page, role) for page in PAGES for role in page.denied_roles]


def get_element_check_cases() -> list[tuple[PageDef, str]]:
    """Generate (page_def, role) pairs that have element checks defined."""
    return [(page, role) for page in PAGES for role in page.checks]
