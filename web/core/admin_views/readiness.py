"""Pre-competition readiness checks and fix handlers for the competition and Competition setup pages."""

import json
import logging
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import cast

from django.conf import settings
from django.core.exceptions import BadRequest
from django.db import models
from django.http import HttpRequest, JsonResponse, StreamingHttpResponse
from django.urls import reverse
from django.utils import timezone
from orange_team.models import OrangeAssignment
from scoring.models import IncidentReport, InjectScore, RedTeamScore

from core.admin_views.places import SetupSection, setup_url
from core.authentik_manager import (
    HASHED_USER_ID,
    SESSIONS_HIDDEN,
    AuthentikAPIError,
    AuthentikGroupEntry,
    AuthentikManager,
    AuthentikUserEntry,
    reason_for,
)
from core.models import AuditLog, CompetitionConfig
from core.services.app_access import check_prestart_app_access
from core.services.user_groups import with_ancestors
from core.task_backends import discord_enabled
from core.team_accounts import (
    accounts_by_team,
    folder_team_number,
    require_single_account,
    team_folder,
    team_folder_users,
)
from core.utils import ndjson_done
from team.models import Team, active_team_numbers, single_account, team_username
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


def _first_five(names: list[str]) -> str:
    """The first five names, and how many more."""
    return ", ".join(names[:5]) + (f"... (+{len(names) - 5})" if len(names) > 5 else "")


def _check_discord_settings() -> CheckResult:
    """Required Discord role/channel/guild IDs must be set in the environment (they default to 0)."""
    from core.utils import missing_discord_settings

    missing = missing_discord_settings()
    if missing:
        return ("fail", f"Missing from the environment: {', '.join(missing)}", None)
    return ("pass", "All required Discord IDs are set", None)


def _check_team_accounts_exist() -> CheckResult:
    """Verify all active team accounts exist in Authentik: each team's one account, or with several accounts per team
    or in team folder mode, at least one in the team's group or the team folder (core.team_accounts)."""
    active_teams = Team.objects.filter(is_active=True)
    if not active_teams.exists():
        return ("warn", "No active teams", None)
    if team_folder() or not single_account():
        return _check_matched_accounts_exist([team.team_number for team in active_teams.order_by("team_number")])

    with AuthentikManager() as mgr:
        existing = {u["username"] for u in mgr.list_all_users()}
    missing = [username for team in active_teams if (username := team_username(team.team_number)) not in existing]

    if missing:
        return (
            "fail",
            f"{len(missing)} missing: {_first_five(missing)}. Create them in Authentik; the portal's token can't.",
            None,
        )
    return ("pass", f"All {active_teams.count()} team accounts exist", None)


def _check_matched_accounts_exist(numbers: list[int]) -> CheckResult:
    """Every team has an account matching its pattern among its group's members, or in the team folder."""
    with AuthentikManager() as mgr:
        accounts = accounts_by_team(mgr, numbers)
    missing = [n for n in numbers if not accounts[n].matched]

    if missing:
        names = _first_five(
            [
                f"team {n:02d}" + (f" (other members: {', '.join(accounts[n].skipped)})" if accounts[n].skipped else "")
                for n in missing
            ]
        )
        where = f"the team folder {settings.TEAM_ACCOUNT_PATH}" if team_folder() else "their group"
        return (
            "fail",
            f"{len(missing)} without an account in {where}: {names}. Create them in Authentik; the portal's token "
            "can't.",
            None,
        )
    return ("pass", f"All {len(numbers)} teams have accounts", None)


def _check_team_group_membership() -> CheckResult:
    """Verify each active team's account is in the team's Authentik group (Team.authentik_group), with one account per
    team and team groups (checks()): with several, a team's accounts are its group's members, and in team folder mode
    there are no groups."""
    active_teams = Team.objects.filter(is_active=True)
    if not active_teams.exists():
        return ("warn", "No active teams", None)

    with AuthentikManager() as mgr:
        users = {u["username"]: u["groups"] for u in mgr.list_all_users()}
        groups = {g["name"]: g["pk"] for g in mgr.list_all_groups()}
    misconfigured: list[str] = []
    for team in active_teams:
        username = team_username(team.team_number)
        if username not in users:
            continue  # handled by account existence check
        group_pk = groups.get(team.authentik_group)
        if not group_pk or group_pk not in users[username]:
            misconfigured.append(username)

    if misconfigured:
        return (
            "fail",
            f"{len(misconfigured)} in wrong group: {_first_five(misconfigured)}",
            {"type": "fix", "key": "fix_group_membership", "label": "Fix Group Membership"},
        )
    return ("pass", "All accounts in correct groups", None)


def _check_usernames_fixed() -> CheckResult:
    """In team folder mode a team comes from the folder plus a full username match, so users must not choose their
    own usernames: a team account could rename itself into another team. Authentik's default user settings flow lets
    a user rename itself when the CAN_CHANGE_USERNAME attribute of the user or one of its groups says so, else by the
    system setting; a custom settings flow can skip both, which no check here sees (docs/self-hosting.md)."""
    with AuthentikManager() as mgr:
        try:
            allowed = mgr.users_may_change_username()
        except AuthentikAPIError as e:
            return (
                "fail",
                f"Could not read Authentik's system settings ({reason_for(e)}); the portal's token needs view System "
                "settings to confirm users can't change their usernames",
                None,
            )
        if allowed:
            return (
                "fail",
                "Authentik lets users change their username (System settings, Allow users to change username): a "
                "team account could rename itself into another team. Turn it off.",
                None,
            )
        folder = team_folder_users(mgr)
        groups = mgr.list_all_groups()
    renamers = _may_rename(folder, groups)
    if renamers:
        return (
            "fail",
            f"{len(renamers)} team folder accounts may change their username through the {CAN_CHANGE_USERNAME} "
            f"attribute, on the account or one of its groups: {_first_five(renamers)}. Remove it.",
            None,
        )
    return ("pass", "Users can't change their Authentik usernames", None)


# The user or group attribute Authentik's default user settings flow reads before the system setting
CAN_CHANGE_USERNAME = "goauthentik.io/user/can-change-username"


def _sets_rename(entry: Mapping[str, object]) -> bool:
    attributes = entry.get("attributes")
    return isinstance(attributes, dict) and bool(attributes.get(CAN_CHANGE_USERNAME))


def _may_rename(users: list[AuthentikUserEntry], groups: list[AuthentikGroupEntry]) -> list[str]:
    """The usernames whose attributes, or whose groups' (with their ancestors), set CAN_CHANGE_USERNAME, sorted."""
    parents = {group["pk"]: group["parents"] for group in groups}
    setting = {group["pk"] for group in groups if _sets_rename(group)}
    return sorted(
        user["username"] for user in users if _sets_rename(user) or with_ancestors(user["groups"], parents) & setting
    )


def _check_sub_mode() -> CheckResult:
    """In team folder mode a sign-in finds its account by uid == sub, which holds only under the hashed user ID
    subject mode."""
    client_id = settings.AUTHENTIK_CLIENT_ID or ""
    with AuthentikManager() as mgr:
        sub_mode = mgr.oauth2_sub_mode(client_id)
    if sub_mode is None:
        return (
            "fail",
            "The portal's token sees no OAuth2 provider with the client ID AUTHENTIK_CLIENT_ID; grant it view on "
            "that provider",
            None,
        )
    if sub_mode != HASHED_USER_ID:
        return (
            "fail",
            f"The portal's OAuth2 provider has subject mode {sub_mode}; team folder sign-ins need "
            '"Based on the User\'s hashed ID" (hashed_user_id), or accounts of one team rename each other',
            None,
        )
    return ("pass", "The portal's OAuth2 provider uses the hashed user ID as sub", None)


def _check_session_access() -> CheckResult:
    """The portal's token must see sessions, or a credentials reset leaves the team's Authentik sessions open."""
    if not single_account():
        return ("pass", "Not needed: teams have several accounts, so the portal resets no team credentials", None)
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
    """Authentik may admit everyone if disabling the BlueTeam binding leaves no enforcing bindings.
    Keep just one BlueTeam binding: Start and Stop toggle only one."""
    config = CompetitionConfig.get_config()
    apps = config.controlled_applications
    if not apps:
        return ("warn", "No controlled apps configured", _configure_apps_link())

    try:
        with AuthentikManager() as mgr:
            # Only shared-account packet sends can enable an account. For other modes check the binding layout.
            numbers = active_team_numbers() if single_account() else []
            names = {team_username(number) for number in numbers}
            users = []
            if names:
                in_folder = team_folder()
                users = [
                    user
                    for user in mgr.list_all_users(with_path=in_folder)
                    if user["username"] in names and (not in_folder or folder_team_number(user) in numbers)
                ]
            check_prestart_app_access(mgr, apps, users, closing=config.applications_enabled)
    except AuthentikAPIError as e:
        return ("fail", reason_for(e), _configure_apps_link())

    if len(users) != len(names) or {user["username"] for user in users} != names:
        return ("fail", "Some active teams' shared accounts could not be checked", _configure_apps_link())
    if not users:
        return ("pass", "Static off-state bindings verified; no shared accounts checked", None)
    return ("pass", f"Off-state bindings for all {len(apps)} apps exclude the {len(users)} shared team accounts", None)


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
    return ("pass", f"Synced {timezone.localtime(cache.last_synced).strftime('%b %d %H:%M')}", None)


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
        names = _first_five([f"Team {t.team_number}" for t in missing])
        return (
            "warn",
            f"{missing.count()} team{'s' if missing.count() != 1 else ''} missing school info: {names}",
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


def checks() -> list[tuple[str, Callable[[], CheckResult]]]:
    """The readiness checks to run: the Discord one only when Discord is configured, the team folder ones only in team
    folder mode, and the group membership one only with one account per team in team groups."""
    discord = [("Discord IDs configured", _check_discord_settings)] if discord_enabled() else []
    folder = (
        [("Usernames fixed", _check_usernames_fixed), ("Sign-in subject mode", _check_sub_mode)]
        if team_folder()
        else []
    )
    group = (
        [("Group membership correct", _check_team_group_membership)] if single_account() and not team_folder() else []
    )
    return [*discord, *folder, ("Team accounts exist", _check_team_accounts_exist), *group, *_CHECKS]


_CHECKS: list[tuple[str, Callable[[], CheckResult]]] = [
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
    all_checks = checks()
    total = len(all_checks)
    fail_count = 0
    warn_count = 0

    for i, (name, check_fn) in enumerate(all_checks, 1):
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


FOLDER_HAS_NO_GROUPS = "Team accounts come from the team folder (TEAM_ACCOUNT_PATH); there are no team groups to fix"


def fix_group_membership(request: HttpRequest, authentik_username: str) -> JsonResponse:
    """Add team accounts to their correct Authentik groups, auditing the ones it changed. Refused in team folder mode,
    where team accounts belong to no group."""
    if team_folder():
        raise BadRequest(FOLDER_HAS_NO_GROUPS)
    require_single_account()
    active_teams = Team.objects.filter(is_active=True)
    fixed: list[str] = []
    failed: list[str] = []
    with AuthentikManager() as mgr:
        try:
            users = {u["username"]: u for u in mgr.list_all_users()}
            groups = {g["name"]: g["pk"] for g in mgr.list_all_groups()}
        except AuthentikAPIError as e:
            return JsonResponse({"success": False, "message": f"Could not ask Authentik: {reason_for(e)}"})

        for team in active_teams:
            username = team_username(team.team_number)
            expected_group = team.authentik_group
            user = users.get(username)
            if not user:
                continue
            group_pk = groups.get(expected_group)
            if not group_pk:
                failed.append(f"{username}: group {expected_group} not found")
                continue
            if group_pk in user["groups"]:
                continue
            try:
                mgr.add_user_to_group(user["pk"], group_pk)
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
