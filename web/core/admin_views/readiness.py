"""Pre-competition readiness checks and fix handlers for the competition page."""

import json
import logging
from collections.abc import Callable, Iterator
from datetime import timedelta
from typing import cast

from django.http import HttpRequest, JsonResponse, StreamingHttpResponse
from django.urls import reverse
from django.utils import timezone

from core.authentik_manager import AuthentikManager
from core.forms import ReadinessFixForm
from core.models import CompetitionConfig
from team.models import Team, team_username

logger = logging.getLogger(__name__)


def _progress(
    step: str,
    current: int,
    total: int,
    severity: str = "pass",
    detail: str = "",
    action: dict[str, str] | None = None,
) -> str:
    """Emit a readiness check progress line as NDJSON."""
    return (
        json.dumps(
            {
                "step": step,
                "current": current,
                "total": total,
                "ok": severity != "fail",
                "severity": severity,
                "detail": detail,
                "action": action,
            }
        )
        + "\n"
    )


# A check returns its severity, a detail message, and an optional fix action.
type CheckResult = tuple[str, str, dict[str, str] | None]


def _check_discord_settings() -> CheckResult:
    """Required Discord role/channel/guild IDs must be set in the environment (they default to 0)."""
    from core.utils import missing_discord_settings

    missing = missing_discord_settings()
    if missing:
        return ("fail", f"Missing from the environment: {', '.join(missing)}", None)
    return ("pass", "All required Discord IDs are set", None)


def _check_team_accounts_exist() -> CheckResult:
    """Verify all active team accounts exist in Authentik."""
    active_teams = Team.objects.filter(is_active=True)
    if not active_teams.exists():
        return ("warn", "No active teams", None)

    mgr = AuthentikManager()
    missing: list[str] = []
    for team in active_teams:
        username = team_username(team.team_number)
        user = mgr.get_user_with_groups(username)
        if not user:
            missing.append(username)

    if missing:
        names = ", ".join(missing[:5])
        suffix = f"... (+{len(missing) - 5})" if len(missing) > 5 else ""
        return (
            "fail",
            f"{len(missing)} missing: {names}{suffix}. Create them in Authentik; the portal's token can't.",
            None,
        )
    return ("pass", f"All {active_teams.count()} team accounts exist", None)


def _check_team_group_membership() -> CheckResult:
    """Verify each teamNN is in the WCComps_BlueTeamNN group."""
    active_teams = Team.objects.filter(is_active=True)
    if not active_teams.exists():
        return ("warn", "No active teams", None)

    mgr = AuthentikManager()
    misconfigured: list[str] = []
    for team in active_teams:
        username = team_username(team.team_number)
        expected_group = team.authentik_group  # e.g. WCComps_BlueTeam01
        user = mgr.get_user_with_groups(username)
        if not user:
            continue  # handled by account existence check

        groups_obj = user.get("groups_obj", [])
        group_names = [g.get("name", "") for g in groups_obj] if isinstance(groups_obj, list) else []
        if expected_group not in group_names:
            misconfigured.append(username)

    if misconfigured:
        names = ", ".join(misconfigured[:5])
        suffix = f"... (+{len(misconfigured) - 5})" if len(misconfigured) > 5 else ""
        return (
            "fail",
            f"{len(misconfigured)} in wrong group: {names}{suffix}",
            {"type": "fix", "key": "fix_group_membership", "label": "Fix Group Membership"},
        )
    return ("pass", "All accounts in correct groups", None)


def _check_blueteam_bindings() -> CheckResult:
    """Verify BlueTeam group bindings exist on all controlled apps."""
    config = CompetitionConfig.get_config()
    apps = config.controlled_applications
    if not apps:
        return ("warn", "No controlled apps configured", {"type": "link", "url": "#setup", "label": "Configure Apps"})

    mgr = AuthentikManager()
    missing: list[str] = []
    for slug in apps:
        app = mgr.get_application_by_slug(slug)
        if not app:
            missing.append(f"{slug} (not found)")
            continue
        binding, _ = mgr.get_blueteam_binding(app["pk"])
        if not binding:
            missing.append(slug)

    if missing:
        return (
            "fail",
            f"Missing bindings: {', '.join(missing)}",
            {"type": "link", "url": "#setup", "label": "Configure Apps"},
        )
    return ("pass", f"Bindings exist for all {len(apps)} apps", None)


def _check_no_tickets() -> CheckResult:
    """Check that no tickets exist yet (clean slate for competition)."""
    from ticketing.models import Ticket

    count = Ticket.objects.count()
    if count > 0:
        return (
            "warn",
            f"{count} ticket{'s' if count != 1 else ''} already exist",
            {"type": "link", "url": reverse("ticket_list"), "label": "View Tickets"},
        )
    return ("pass", "No existing tickets", None)


def _check_packets_distributed() -> CheckResult:
    """Check that all completed packets were distributed to all active teams."""
    from packets.models import Packet, PacketDistribution

    active_teams = Team.objects.filter(is_active=True)
    if not active_teams.exists():
        return ("warn", "No active teams", None)

    completed_packets = Packet.objects.filter(status="completed")
    if not completed_packets.exists():
        return ("pass", "No completed packets to check", None)

    issues: list[str] = []
    for packet in completed_packets:
        distributed_team_ids = set(
            PacketDistribution.objects.filter(
                packet=packet,
                email_status__in=["sent", "delivered"],
            ).values_list("team_id", flat=True)
        )
        missing = active_teams.exclude(id__in=distributed_team_ids).count()
        if missing > 0:
            issues.append(f"{packet.title}: {missing} teams missing")

    if issues:
        return (
            "warn",
            "; ".join(issues),
            {"type": "link", "url": reverse("packets_list"), "label": "View Packets"},
        )
    return ("pass", "All packets distributed to all teams", None)


def _check_start_message() -> CheckResult:
    """The start message (default VM credentials) goes to every team when the competition starts."""
    if not CompetitionConfig.get_config().start_message.strip():
        return ("warn", "No start message: teams get no message when the competition starts", None)
    return ("pass", "Start message set", None)


def _check_quotient_synced() -> CheckResult:
    """Check that Quotient metadata was synced recently."""
    from scoring.models import QuotientMetadataCache

    cache = QuotientMetadataCache.objects.first()
    if not cache:
        return (
            "warn",
            "Quotient metadata not synced yet",
            {"type": "fix", "key": "fix_sync_quotient", "label": "Sync Now"},
        )

    age = timezone.now() - cache.last_synced
    if age > timedelta(days=7):
        days = age.days
        return (
            "warn",
            f"Last synced {days} days ago",
            {"type": "fix", "key": "fix_sync_quotient", "label": "Sync Now"},
        )
    return ("pass", f"Synced {cache.last_synced.strftime('%b %d %H:%M')}", None)


def _check_no_red_scores() -> CheckResult:
    """Check that no red team findings exist yet (clean slate for competition)."""
    from scoring.models import RedTeamScore

    count = RedTeamScore.objects.count()
    if count > 0:
        return (
            "warn",
            f"{count} finding{'s' if count != 1 else ''} already exist",
            {"type": "link", "url": reverse("scoring:red_team_findings"), "label": "View Findings"},
        )
    return ("pass", "No existing findings", None)


def _check_no_incidents() -> CheckResult:
    """Check that no incident reports exist yet (clean slate for competition)."""
    from scoring.models import IncidentReport

    count = IncidentReport.objects.count()
    if count > 0:
        return (
            "warn",
            f"{count} report{'s' if count != 1 else ''} already exist",
            {"type": "link", "url": reverse("scoring:review_incidents"), "label": "View Reports"},
        )
    return ("pass", "No existing incident reports", None)


def _check_no_inject_grades() -> CheckResult:
    """Check that no inject grades exist yet (clean slate for competition)."""
    from scoring.models import InjectScore

    count = InjectScore.objects.count()
    if count > 0:
        return (
            "warn",
            f"{count} grade{'s' if count != 1 else ''} already exist",
            {"type": "link", "url": reverse("scoring:inject_grades_review"), "label": "View Grades"},
        )
    return ("pass", "No existing inject grades", None)


def _check_no_orange_assignments() -> CheckResult:
    """Check that no orange team assignments exist yet (clean slate for competition)."""
    from orange_team.models import OrangeAssignment

    count = OrangeAssignment.objects.count()
    if count > 0:
        return (
            "warn",
            f"{count} assignment{'s' if count != 1 else ''} already exist",
            {"type": "link", "url": reverse("review_queue"), "label": "View Assignments"},
        )
    return ("pass", "No existing orange assignments", None)


def _check_quotient_api() -> CheckResult:
    """Check that Quotient scoring engine API is reachable."""
    from quotient.client import QuotientClient

    client = QuotientClient()
    try:
        infra = client.get_infrastructure(force_refresh=True)
    except Exception as e:
        return ("fail", f"API error: {e}", None)

    if not infra:
        return ("fail", "Could not reach Quotient API", None)
    return ("pass", f"Connected — {len(infra.boxes)} boxes, {infra.team_count} teams", None)


def _check_school_info() -> CheckResult:
    """Check that all active teams have school info with contact emails."""
    from team.models import SchoolInfo

    active_teams = Team.objects.filter(is_active=True)
    if not active_teams.exists():
        return ("warn", "No active teams", None)

    teams_with_info = set(SchoolInfo.objects.filter(team__is_active=True).values_list("team_id", flat=True))
    missing = active_teams.exclude(id__in=teams_with_info)
    if missing.exists():
        names = ", ".join(f"Team {t.team_number}" for t in missing[:5])
        suffix = f"... (+{missing.count() - 5})" if missing.count() > 5 else ""
        return (
            "warn",
            f"{missing.count()} team{'s' if missing.count() != 1 else ''} missing school info: {names}{suffix}",
            {"type": "link", "url": reverse("school_info"), "label": "Manage Schools"},
        )
    return ("pass", f"All {active_teams.count()} teams have school info", None)


def _check_multiple_teams() -> CheckResult:
    """Check that more than one team is registered."""
    active_count = Team.objects.filter(is_active=True).count()
    if active_count == 0:
        return ("fail", "No active teams registered", None)
    if active_count == 1:
        return ("warn", "Only 1 team registered", None)
    return ("pass", f"{active_count} teams registered", None)


ALL_CHECKS: list[tuple[str, Callable[[], CheckResult]]] = [
    # Phase 1: Authentik
    ("Discord IDs configured", _check_discord_settings),
    ("Team accounts exist", _check_team_accounts_exist),
    ("Group membership correct", _check_team_group_membership),
    ("App bindings configured", _check_blueteam_bindings),
    # Phase 2: Teams & infrastructure
    ("Multiple teams registered", _check_multiple_teams),
    ("School info configured", _check_school_info),
    ("Quotient API reachable", _check_quotient_api),
    ("Quotient metadata synced", _check_quotient_synced),
    ("Packets distributed", _check_packets_distributed),
    ("Start message set", _check_start_message),
    # Phase 3: Clean slate
    ("No existing tickets", _check_no_tickets),
    ("No existing red team findings", _check_no_red_scores),
    ("No existing incident reports", _check_no_incidents),
    ("No existing inject grades", _check_no_inject_grades),
    ("No existing orange assignments", _check_no_orange_assignments),
]


def stream_readiness_checks() -> Iterator[str]:
    """Run all readiness checks and yield NDJSON progress lines."""
    total = len(ALL_CHECKS)
    fail_count = 0
    warn_count = 0

    for i, (name, check_fn) in enumerate(ALL_CHECKS, 1):
        try:
            severity, detail, action = check_fn()
        except Exception as e:
            severity, detail, action = "fail", f"Check error: {e}", None

        if severity == "fail":
            fail_count += 1
        elif severity == "warn":
            warn_count += 1

        yield _progress(name, i, total, severity, detail, action)

    if fail_count > 0:
        msg = f"{fail_count} failed, {warn_count} warning(s)"
        success = False
    elif warn_count > 0:
        msg = f"All passed with {warn_count} warning(s)"
        success = True
    else:
        msg = "All checks passed"
        success = True

    yield json.dumps({"done": True, "success": success, "message": msg}) + "\n"


def _fix_group_membership(request: HttpRequest) -> JsonResponse:
    """Add team accounts to their correct Authentik groups."""
    active_teams = Team.objects.filter(is_active=True)
    mgr = AuthentikManager()

    fixed = 0
    failed: list[str] = []
    for team in active_teams:
        username = team_username(team.team_number)
        expected_group = team.authentik_group
        user = mgr.get_user_with_groups(username)
        if not user:
            continue

        groups_obj = user.get("groups_obj", [])
        group_names = [g.get("name", "") for g in groups_obj] if isinstance(groups_obj, list) else []
        if expected_group in group_names:
            continue

        group = mgr.get_group_by_name(expected_group)
        if not group:
            failed.append(f"{username}: group {expected_group} not found")
            continue

        success, error = mgr.add_user_to_group(cast(int, user["pk"]), cast(str, group["pk"]))
        if success:
            fixed += 1
        else:
            failed.append(f"{username}: {error}")

    msg = f"Fixed {fixed} account(s)"
    if failed:
        msg += f", {len(failed)} failed"
    return JsonResponse({"success": True, "message": msg, "failed": failed})


def _fix_sync_quotient(request: HttpRequest) -> JsonResponse:
    from scoring.quotient_sync import sync_quotient_metadata

    try:
        sync_quotient_metadata()
        return JsonResponse({"success": True, "message": "Quotient metadata synced"})
    except Exception as e:
        logger.exception(f"Failed to sync Quotient metadata: {e}")
        return JsonResponse({"error": f"Sync failed: {e}"}, status=500)


_FIX_HANDLERS: dict[str, Callable[[HttpRequest], JsonResponse]] = {
    "fix_group_membership": _fix_group_membership,
    "fix_sync_quotient": _fix_sync_quotient,
}


# Called from the competition.py action dispatcher.
def action_readiness_check(
    request: HttpRequest,
    config: CompetitionConfig,
    authentik_username: str,
) -> StreamingHttpResponse:
    return StreamingHttpResponse(
        stream_readiness_checks(),
        content_type="application/x-ndjson",
    )


def action_readiness_fix(
    request: HttpRequest,
    config: CompetitionConfig,
    authentik_username: str,
) -> JsonResponse:
    form = ReadinessFixForm(request.POST)
    if not form.is_valid():
        return JsonResponse({"error": "Missing fix parameter"}, status=400)
    fix_key = form.cleaned_data["fix"]
    handler = _FIX_HANDLERS.get(fix_key)
    if not handler:
        return JsonResponse({"error": f"Unknown fix: {fix_key}"}, status=400)
    return handler(request)
