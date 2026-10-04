"""Context processors for making data available to all templates."""

from django.contrib.auth.models import User
from django.http import HttpRequest

from .auth_utils import get_permissions_context, has_permission
from .permission_constants import PERMISSION_MAP

# Maps Django URL names to (nav_section, subnav_section) for navigation highlighting.
# IMPORTANT: When adding new URL patterns, add an entry here or the nav item
# won't highlight. test_all_nav_mapping_url_names_exist catches stale entries.
NAV_MAPPING: dict[str, tuple[str, str]] = {
    # Tickets - unified ticket management
    "ticket_list": ("tickets", ""),
    "ticket_detail": ("tickets", ""),
    "ticket_claim": ("tickets", ""),
    "ticket_unclaim": ("tickets", ""),
    "ticket_resolve": ("tickets", ""),
    "ticket_reopen": ("tickets", ""),
    "ticket_reassign": ("tickets", ""),
    "ticket_comment": ("tickets", ""),
    "ticket_change_category": ("tickets", ""),
    "ticket_attachment_upload": ("tickets", ""),
    "ticket_attachment_download": ("tickets", ""),
    "tickets_bulk_claim": ("tickets", ""),
    "tickets_bulk_resolve": ("tickets", ""),
    "tickets_clear_all": ("tickets", ""),
    "ticket_detail_dynamic": ("tickets", ""),
    "ticket_notifications": ("tickets", ""),
    "create_ticket": ("tickets", ""),
    "ticket_cancel": ("tickets", ""),
    # Incident Report (blue team)
    "submit_incident_report": ("incident", ""),
    "view_incident_report": ("incident", ""),
    "incident_list": ("incident", ""),
    "delete_incident_report": ("incident", ""),
    # Red Team Findings
    "submit_red_score": ("red_findings", "submit"),
    "red_team_scores": ("red_findings", "findings"),
    "view_red_score": ("red_findings", "findings"),
    "delete_red_score": ("red_findings", "findings"),
    "leave_red_score": ("red_findings", "findings"),
    "ip_pool_list": ("red_findings", "pools"),
    "ip_pool_create": ("red_findings", "pools"),
    "ip_pool_edit": ("red_findings", "pools"),
    "ip_pool_delete": ("red_findings", "pools"),
    # Orange Team check approval views (scoring app)
    "bulk_approve_orange_adjustments": ("orange", "portal"),
    # Orange Team Challenges (orange_team app)
    "dashboard": ("orange", "dashboard"),
    "review_queue": ("orange", "review"),
    "check_list": ("orange", "checks"),
    "check_create": ("orange", "checks"),
    "check_detail": ("orange", "checks"),
    "check_edit": ("orange", "checks"),
    "check_duplicate": ("orange", "checks"),
    "check_assign": ("orange", "checks"),
    "assignment_save": ("orange", "dashboard"),
    "assignment_submit": ("orange", "dashboard"),
    "assignment_approve": ("orange", "review"),
    "assignment_reject": ("orange", "review"),
    "export_scores": ("orange", "review"),
    "followup_create": ("orange", "dashboard"),
    "followup_dismiss": ("orange", "dashboard"),
    "toggle_checkin": ("orange", "dashboard"),
    "admin_toggle_checkin": ("orange", "checkins"),
    # Ops Admin
    "admin_competition": ("ops_admin", "competition"),
    "admin_competition_action": ("ops_admin", "competition"),
    "admin_competition_danger": ("ops_admin", "competition"),
    "admin_teams": ("ops_admin", "teams"),
    "admin_teams_bulk_action": ("ops_admin", "teams"),
    "admin_team_detail": ("ops_admin", "teams"),
    "admin_team_action": ("ops_admin", "teams"),
    "admin_broadcast": ("ops_admin", "broadcast"),
    "admin_broadcast_action": ("ops_admin", "broadcast"),
    "admin_sync_roles": ("ops_admin", "sync"),
    "admin_sync_roles_action": ("ops_admin", "sync"),
    "admin_categories": ("ops_admin", "categories"),
    "admin_category_create": ("ops_admin", "categories"),
    "admin_category_edit": ("ops_admin", "categories"),
    "admin_category_delete": ("ops_admin", "categories"),
    "admin_task_status": ("ops_admin", ""),
    "school_info": ("ops_admin", "schools"),
    "school_info_edit": ("ops_admin", "schools"),
    "school_info_import": ("ops_admin", "schools"),
    "packets_list": ("ops_admin", "packets"),
    "upload_packet": ("ops_admin", "packets"),
    "packet_detail": ("ops_admin", "packets"),
    "packet_action": ("ops_admin", "packets"),
    "packet_resend_team": ("ops_admin", "packets"),
    "team_packet": ("ops_admin", "packets"),
    # Registration admin
    "registration_edit": ("registration", ""),
    "registration_review_list": ("registration", "registrations"),
    "registration_approve": ("registration", "registrations"),
    "registration_reject": ("registration", "registrations"),
    "registration_mark_paid": ("registration", "registrations"),
    "registration_season_list": ("registration", "seasons"),
    "registration_season_create": ("registration", "seasons"),
    "registration_season_edit": ("registration", "seasons"),
    "registration_season_delete": ("registration", "seasons"),
    "registration_event_list": ("registration", "seasons"),
    "registration_event_create": ("registration", "seasons"),
    "registration_event_detail": ("registration", "seasons"),
    "registration_event_edit": ("registration", "seasons"),
    "registration_event_delete": ("registration", "seasons"),
    "registration_assign_teams": ("registration", "seasons"),
    "registration_unassign_team": ("registration", "seasons"),
    # Leaderboard
    "leaderboard": ("leaderboard", ""),
    "scorecard": ("leaderboard", ""),
    "leaderboard_page": ("leaderboard", ""),
    "leaderboard_scorecard": ("leaderboard", ""),
    "leaderboard_scorecard_pdf": ("leaderboard", ""),
    # Scoring review pages
    "red_team_findings": ("scoring", "red_team"),
    "bulk_approve_red_scores": ("scoring", "red_team"),
    "review_orange": ("scoring", "orange_team"),
    "review_incidents": ("scoring", "review_incidents"),
    "match_incident": ("scoring", "review_incidents"),
    "inject_grades_review": ("scoring", "inject_grades_review"),
    "inject_grades_bulk_approve": ("scoring", "inject_grades_review"),
    "ops_review_tickets": ("scoring", "review_tickets"),
    "ops_verify_ticket": ("scoring", "review_tickets"),
    "ops_batch_verify_tickets": ("scoring", "review_tickets"),
    # White team - inject grading/feedback
    "inject_grading": ("white_team", "inject_grading"),
    "review_inject_feedback": ("white_team", "inject_feedback"),
    # Ops admin - scoring config/export
    "scoring_config": ("ops_admin", "config"),
    "export_index": ("ops_admin", "export"),
    "export_red_scores": ("ops_admin", "export"),
    "export_tickets": ("ops_admin", "export"),
    "export_incidents": ("ops_admin", "export"),
}


def _get_nav_active(request: HttpRequest) -> dict[str, str]:
    """Determine which nav items should be active based on URL."""
    url_name = getattr(request.resolver_match, "url_name", "") or ""
    app_name = getattr(request.resolver_match, "app_name", "") or ""

    if url_name in NAV_MAPPING:
        nav, subnav = NAV_MAPPING[url_name]
        return {"nav": nav, "subnav": subnav}

    # Fallback for orange_team app (covers dashboard, export_scores, etc.)
    if app_name == "orange_team":
        return {"nav": "orange", "subnav": ""}

    if app_name == "admin":
        return {"nav": "admin", "subnav": ""}

    return {"nav": "", "subnav": ""}


def permissions(request: HttpRequest) -> dict[str, bool | str]:
    """Add permission flags to all template contexts."""
    if not request.user.is_authenticated:
        ctx: dict[str, bool | str] = {f"is_{perm}": False for perm in PERMISSION_MAP}
        ctx.update(
            {
                "is_blue_team": False,
                "is_red_team": False,
                "authentik_username": "",
                "nav_active": "",
                "subnav_active": "",
            }
        )
        return ctx

    user: User = request.user
    perms = get_permissions_context(user)
    nav_context = _get_nav_active(request)
    return {
        **perms,
        "is_blue_team": has_permission(user, "blue_team"),
        "is_red_team": has_permission(user, "red_team"),
        "authentik_username": user.username,
        "nav_active": nav_context["nav"],
        "subnav_active": nav_context["subnav"],
    }
