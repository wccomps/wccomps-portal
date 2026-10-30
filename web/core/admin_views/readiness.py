"""Pre-competition readiness checks and fix handlers for the competition and Competition setup pages."""

import json
import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import timedelta
from typing import cast

from django.db import models
from django.http import HttpRequest, JsonResponse, StreamingHttpResponse
from django.urls import reverse
from django.utils import timezone
from orange_team.models import OrangeAssignment
from scoring.models import IncidentReport, InjectScore, RedTeamScore

from core.admin_views.places import SetupSection, setup_url
from core.authentik_manager import SESSIONS_HIDDEN, AuthentikAPIError, AuthentikManager, reason_for
from core.models import AuditLog, CompetitionConfig
from core.utils import ndjson_done
from team.models import Team, team_username
from ticketing.models import Ticket

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

        if all(g["name"] != expected_group for g in user["groups_obj"]):
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


def _check_session_access() -> CheckResult:
    """The portal's token must see sessions, or a credentials reset leaves the team's Authentik sessions open."""
    with AuthentikManager() as manager:
        visible = manager.sees_any_sessions()
    if visible:
        return ("pass", "The portal's token can see Authentik sessions", None)
    return (
        "fail",
        f"{SESSIONS_HIDDEN} Sign in to Authentik if you aren't, then run the checks again. The token also needs the "
        "delete permission, which this check can't test.",
        None,
    )


def _setup_link(section: SetupSection, label: str) -> dict[str, str]:
    """A link to the Competition setup section where a failed check is fixed."""
    return {"type": "link", "url": setup_url(section), "label": label}


def _configure_apps_link() -> dict[str, str]:
    return _setup_link(SetupSection.TEAM_ACCESS, "Configure Apps")


def _check_blueteam_bindings() -> CheckResult:
    """Verify BlueTeam group bindings exist on all controlled apps."""
    config = CompetitionConfig.get_config()
    apps = config.controlled_applications
    if not apps:
        return ("warn", "No controlled apps configured", _configure_apps_link())

    mgr = AuthentikManager()
    missing: list[str] = []
    for slug in apps:
        app = mgr.get_application_by_slug(slug)
        if not app:
            missing.append(f"{slug} (not found)")
            continue
        if not mgr.get_blueteam_binding(app["pk"]):
            missing.append(slug)

    if missing:
        return (
            "fail",
            f"Missing bindings: {', '.join(missing)}",
            _configure_apps_link(),
        )
    return ("pass", f"Bindings exist for all {len(apps)} apps", None)


def _check_packets_distributed() -> CheckResult:
    """Check that packets went out for the open event and that every team on the school list has a delivered copy
    of each. Earlier events' packets went to their own teams."""
    from packets.models import Packet
    from packets.services import teams_without_delivered_copy
    from registration.models import Event

    event = Event.objects.open()
    if event is None:
        return ("warn", "No event is open, so no packet can be sent", None)
    packets_link = {"type": "link", "url": reverse("packets_list"), "label": "View Packets"}
    sent = Packet.objects.sent().filter(event=event)
    if not sent.exists():
        return ("warn", f"No packet has been sent for {event}", packets_link)

    missing = [
        (packet.title, teams_without_delivered_copy(Packet.objects.filter(pk=packet.pk)).count()) for packet in sent
    ]
    issues = [
        f"{title}: {count} team{'s' if count != 1 else ''} without a delivered copy"
        for title, count in missing
        if count
    ]
    if issues:
        return ("warn", "; ".join(issues), packets_link)
    return ("pass", "Every team on the list has a delivered copy of each sent packet", None)


def _check_start_message() -> CheckResult:
    """The start message (default VM credentials) goes to every team when the competition starts."""
    if not CompetitionConfig.get_config().start_message.strip():
        return (
            "warn",
            "No start message: teams get no message when the competition starts",
            _setup_link(SetupSection.START_MESSAGE, "Set Start Message"),
        )
    return ("pass", "Start message set", None)


def _check_quotient_synced() -> CheckResult:
    """Check that Quotient metadata was synced recently."""
    from scoring.models import QuotientMetadataCache

    cache = QuotientMetadataCache.objects.first()
    if not cache:
        return (
            "warn",
            "Quotient metadata not synced yet",
            {"type": "fix", "key": "sync_quotient", "label": "Sync Now"},
        )

    age = timezone.now() - cache.last_synced
    if age > timedelta(days=7):
        days = age.days
        return (
            "warn",
            f"Last synced {days} days ago",
            {"type": "fix", "key": "sync_quotient", "label": "Sync Now"},
        )
    return ("pass", f"Synced {cache.last_synced.strftime('%b %d %H:%M')}", None)


@dataclass(frozen=True)
class CleanSlateCheck:
    step_name: str
    model: type[models.Model]
    singular: str
    pass_label: str
    url_name: str
    link_label: str

    def __call__(self) -> CheckResult:
        count = self.model._default_manager.count()
        if count > 0:
            return (
                "warn",
                f"{count} {self.singular}{'s' if count != 1 else ''} already exist",
                {"type": "link", "url": reverse(self.url_name), "label": self.link_label},
            )
        return ("pass", f"No existing {self.pass_label}", None)


CLEAN_SLATE_CHECKS: tuple[CleanSlateCheck, ...] = (
    CleanSlateCheck("No existing tickets", Ticket, "ticket", "tickets", "ticket_list", "View Tickets"),
    CleanSlateCheck(
        "No existing red team findings",
        RedTeamScore,
        "finding",
        "findings",
        "scoring:red_team_findings",
        "View Findings",
    ),
    CleanSlateCheck(
        "No existing incident reports",
        IncidentReport,
        "report",
        "incident reports",
        "scoring:review_incidents",
        "View Reports",
    ),
    CleanSlateCheck(
        "No existing inject grades",
        InjectScore,
        "grade",
        "inject grades",
        "scoring:inject_grades_review",
        "View Grades",
    ),
    CleanSlateCheck(
        "No existing orange assignments",
        OrangeAssignment,
        "assignment",
        "orange assignments",
        "review_queue",
        "View Assignments",
    ),
)


def _check_quotient_api() -> CheckResult:
    """Check that Quotient scoring engine API is reachable."""
    from quotient.client import QuotientClient

    client = QuotientClient()
    try:
        infra = client.get_infrastructure(force_refresh=True)
    except Exception as e:
        return ("fail", f"API error: {e}", _setup_link(SetupSection.SCORING_ENGINE, "Scoring Engine"))

    if not infra:
        return ("fail", "Could not reach Quotient API", _setup_link(SetupSection.SCORING_ENGINE, "Scoring Engine"))
    return ("pass", f"Connected: {len(infra.boxes)} boxes", None)


def _check_school_info() -> CheckResult:
    """Check that all active teams have a SchoolInfo record."""
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
    ("Discord IDs configured", _check_discord_settings),
    ("Team accounts exist", _check_team_accounts_exist),
    ("Group membership correct", _check_team_group_membership),
    ("Authentik sessions visible", _check_session_access),
    ("App bindings configured", _check_blueteam_bindings),
    ("Multiple teams registered", _check_multiple_teams),
    ("School info configured", _check_school_info),
    ("Quotient API reachable", _check_quotient_api),
    ("Quotient metadata synced", _check_quotient_synced),
    ("Packets distributed", _check_packets_distributed),
    ("Start message set", _check_start_message),
    *((check.step_name, check) for check in CLEAN_SLATE_CHECKS),
]


def stream_readiness_checks() -> Iterator[str]:
    """Run all readiness checks and yield NDJSON progress lines."""
    total = len(ALL_CHECKS)
    fail_count = 0
    warn_count = 0

    for i, (name, check_fn) in enumerate(ALL_CHECKS, 1):
        try:
            severity, detail, action = check_fn()
        except AuthentikAPIError as e:
            severity, detail, action = "fail", f"Could not ask Authentik: {reason_for(e)}", None
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

    yield ndjson_done(msg, success=success)


def fix_group_membership(request: HttpRequest, authentik_username: str) -> JsonResponse:
    """Add team accounts to their correct Authentik groups, auditing the ones it changed."""
    active_teams = Team.objects.filter(is_active=True)
    mgr = AuthentikManager()

    fixed: list[str] = []
    failed: list[str] = []
    for team in active_teams:
        username = team_username(team.team_number)
        expected_group = team.authentik_group
        try:
            user = mgr.get_user_with_groups(username)
            if not user or any(g["name"] == expected_group for g in user["groups_obj"]):
                continue
            group = mgr.get_group_by_name(expected_group)
            if not group:
                failed.append(f"{username}: group {expected_group} not found")
                continue
            mgr.add_user_to_group(user["pk"], group["pk"])
        except AuthentikAPIError as e:
            failed.append(f"{username}: {reason_for(e)}")
            continue
        fixed.append(username)

    if fixed:
        AuditLog.objects.create(
            action="team_groups_fixed",
            admin_user=authentik_username,
            target_entity="team",
            target_id=0,
            details={"fixed": fixed, "failed": failed},
        )
    msg = f"Fixed {len(fixed)} account(s)"
    if failed:
        msg += "; failed: " + "; ".join(failed)
    return JsonResponse({"success": not failed, "message": msg})


READINESS_FIXES = frozenset({"fix_group_membership", "sync_quotient"})


def sync_quotient(request: HttpRequest, authentik_username: str) -> JsonResponse:
    """Sync Quotient's metadata; an unreachable Quotient is a 200 whose message says why."""
    from django.contrib.auth.models import User
    from scoring.quotient_sync import sync_quotient_metadata

    try:
        sync_quotient_metadata(cast(User, request.user))
    except ValueError as e:
        logger.warning(f"Failed to sync Quotient metadata: {e}")
        return JsonResponse({"success": False, "message": f"Sync failed: {e}"})
    AuditLog.objects.create(
        action="quotient_metadata_synced",
        admin_user=authentik_username,
        target_entity="quotient",
        target_id=0,
        details={},
    )
    return JsonResponse({"success": True, "message": "Quotient metadata synced"})


def action_readiness_check(
    request: HttpRequest,
    authentik_username: str,
) -> StreamingHttpResponse:
    return StreamingHttpResponse(
        stream_readiness_checks(),
        content_type="application/x-ndjson",
    )
