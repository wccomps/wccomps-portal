import csv
import io
import json
import logging
from collections.abc import Iterator
from typing import cast

from django.contrib.auth.models import User
from django.db import transaction
from django.http import HttpRequest, HttpResponse, HttpResponseBase, JsonResponse, StreamingHttpResponse
from django.shortcuts import render
from scoring.models import QuotientMetadataCache
from scoring.quotient_sync import sync_quotient_metadata

from core.admin_views.readiness import action_readiness_check, action_readiness_fix
from core.authentik_manager import AuthentikManager
from core.authentik_utils import reset_team_credentials
from core.discord_tasks import CleanupCompetition, LogToChannel
from core.forms import ActionForm, AppSlugForm, ResetPasswordsForm, SetMaxMembersForm, SetTimeForm
from core.models import AuditLog, CompetitionConfig, DiscordTask
from core.services.competition import CompetitionRunResult, run_competition
from core.utils import ndjson_progress as _progress
from core.utils import run_detached
from team.models import active_team_numbers, team_username

from ..auth_utils import has_permission, require_permission
from ..utils import UnknownTimezoneError, parse_datetime_to_utc

logger = logging.getLogger(__name__)

TIMEZONE_CHOICES = [
    ("America/Los_Angeles", "Pacific Time (PT)"),
    ("America/Denver", "Mountain Time (MT)"),
    ("America/Chicago", "Central Time (CT)"),
    ("America/New_York", "Eastern Time (ET)"),
    ("UTC", "UTC"),
]


def _has_admin_or_gold_access(user: User) -> bool:
    return has_permission(user, "admin") or has_permission(user, "gold_team")


def _action_set_max_members(request: HttpRequest, config: CompetitionConfig, authentik_username: str) -> JsonResponse:
    form = SetMaxMembersForm(request.POST)
    if not form.is_valid():
        return JsonResponse({"error": "Max members must be 1-20"}, status=400)

    max_members = form.cleaned_data["max_members"]
    old_max = config.max_team_members
    config.max_team_members = max_members
    config.save()

    AuditLog.objects.create(
        action="max_team_members_updated",
        admin_user=authentik_username,
        target_entity="competition_config",
        target_id=config.pk,
        details={"old_max": old_max, "new_max": max_members},
    )

    return JsonResponse({"success": True, "message": f"Max members set to {max_members}"})


def _edit_controlled_app(
    request: HttpRequest, config: CompetitionConfig, authentik_username: str, *, add: bool
) -> JsonResponse:
    """Add or remove one controlled app against the CURRENT stored list, so a stale page can't drop others."""
    form = AppSlugForm(request.POST)
    if not form.is_valid():
        return JsonResponse({"error": "Please provide an app slug"}, status=400)
    slug = form.cleaned_data["app_slug"]
    # Check with the lookup Start and Stop use; Authentik's app list omits apps the service account can't open
    if add and AuthentikManager().get_application_by_slug(slug) is None:
        return JsonResponse({"error": f"No Authentik application with the slug '{slug}'"}, status=400)

    with transaction.atomic():
        config = CompetitionConfig.objects.select_for_update().get(pk=config.pk)
        apps = list(config.controlled_applications or [])
        if add and slug not in apps:
            apps.append(slug)
        elif not add and slug in apps:
            apps.remove(slug)
        config.controlled_applications = apps
        config.save(update_fields=["controlled_applications", "updated_at"])

        AuditLog.objects.create(
            action="competition_apps_configured",
            admin_user=authentik_username,
            target_entity="competition_config",
            target_id=config.pk,
            details={"controlled_apps": apps, "added" if add else "removed": slug},
        )

    verb = "Added" if add else "Removed"
    return JsonResponse({"success": True, "message": f"{verb} {slug}", "apps": apps})


def _action_add_app(request: HttpRequest, config: CompetitionConfig, authentik_username: str) -> JsonResponse:
    return _edit_controlled_app(request, config, authentik_username, add=True)


def _action_remove_app(request: HttpRequest, config: CompetitionConfig, authentik_username: str) -> JsonResponse:
    return _edit_controlled_app(request, config, authentik_username, add=False)


def _action_set_start_time(request: HttpRequest, config: CompetitionConfig, authentik_username: str) -> JsonResponse:
    form = SetTimeForm(request.POST)
    if not form.is_valid():
        return JsonResponse({"error": "Please provide a datetime"}, status=400)

    datetime_str = form.cleaned_data["datetime"]
    tz_name = form.cleaned_data["timezone"]

    try:
        start_time = parse_datetime_to_utc(datetime_str, tz_name)
        if error := config.schedule_error(start=start_time):
            return JsonResponse({"error": error}, status=400)

        config.competition_start_time = start_time
        config.save()

        AuditLog.objects.create(
            action="competition_start_time_set",
            admin_user=authentik_username,
            target_entity="competition_config",
            target_id=config.pk,
            details={"start_time": start_time.isoformat(), "controlled_apps": config.controlled_applications},
        )

        return JsonResponse({"success": True, "message": f"Start time set to {start_time.isoformat()}"})
    except UnknownTimezoneError as e:
        return JsonResponse({"error": str(e)}, status=400)
    except ValueError:
        return JsonResponse({"error": "Invalid datetime format"}, status=400)


def _action_set_end_time(request: HttpRequest, config: CompetitionConfig, authentik_username: str) -> JsonResponse:
    form = SetTimeForm(request.POST)
    if not form.is_valid():
        return JsonResponse({"error": "Please provide a datetime"}, status=400)

    datetime_str = form.cleaned_data["datetime"]
    tz_name = form.cleaned_data["timezone"]

    try:
        end_time = parse_datetime_to_utc(datetime_str, tz_name)
        if error := config.schedule_error(end=end_time):
            return JsonResponse({"error": error}, status=400)

        config.competition_end_time = end_time
        config.save()

        AuditLog.objects.create(
            action="competition_end_time_set",
            admin_user=authentik_username,
            target_entity="competition_config",
            target_id=config.pk,
            details={"end_time": end_time.isoformat(), "controlled_apps": config.controlled_applications},
        )

        return JsonResponse({"success": True, "message": f"End time set to {end_time.isoformat()}"})
    except UnknownTimezoneError as e:
        return JsonResponse({"error": str(e)}, status=400)
    except ValueError:
        return JsonResponse({"error": "Invalid datetime format"}, status=400)


def _action_set_schedule(request: HttpRequest, config: CompetitionConfig, authentik_username: str) -> JsonResponse:
    """Handle set_schedule action — set start and/or end time in one request."""
    from core.forms import SetScheduleForm

    form = SetScheduleForm(request.POST)
    if not form.is_valid():
        first_error = next(iter(form.errors.values()), ["Invalid schedule data"])
        return JsonResponse({"error": str(first_error[0])}, status=400)

    start_dt = form.cleaned_data["start_datetime"]
    start_tz = form.cleaned_data.get("start_timezone") or "America/Los_Angeles"
    end_dt = form.cleaned_data["end_datetime"]
    end_tz = form.cleaned_data.get("end_timezone") or "America/Los_Angeles"

    try:
        start_time = parse_datetime_to_utc(start_dt, start_tz) if start_dt else None
        end_time = parse_datetime_to_utc(end_dt, end_tz) if end_dt else None
    except UnknownTimezoneError as e:
        return JsonResponse({"error": str(e)}, status=400)
    except ValueError:
        return JsonResponse({"error": "Invalid datetime format"}, status=400)
    if error := config.schedule_error(start_time, end_time):
        return JsonResponse({"error": error}, status=400)

    details: dict[str, str] = {}
    if start_time:
        config.competition_start_time = start_time
        details["start_time"] = start_time.isoformat()
    if end_time:
        config.competition_end_time = end_time
        details["end_time"] = end_time.isoformat()
    config.save()

    AuditLog.objects.create(
        action="competition_schedule_set",
        admin_user=authentik_username,
        target_entity="competition_config",
        target_id=config.pk,
        details={**details, "controlled_apps": config.controlled_applications},
    )

    parts = [
        f"start={details['start_time']}" if "start_time" in details else "",
        f"end={details['end_time']}" if "end_time" in details else "",
    ]
    msg = "Schedule updated: " + ", ".join(p for p in parts if p)
    return JsonResponse({"success": True, "message": msg})


def _stream_competition_run(enable: bool, authentik_username: str) -> Iterator[str]:
    """Stream run_competition's progress as NDJSON, then tell the ops channel how it went."""
    steps = run_competition(enable, authentik_username)
    while True:
        try:
            step = next(steps)
        except StopIteration as done:
            result: CompetitionRunResult = done.value
            break
        yield _progress(step.message, step.current, step.total, ok=step.ok)

    if result.success:
        verb = "Started" if enable else "Stopped"
        incomplete = " (incomplete)" if result.has_failures else ""
        DiscordTask.enqueue(
            LogToChannel(
                message=f"**Competition {verb}{incomplete}** by {authentik_username} (web)\n{result.summary()}"
            )
        )
    # A partial run is not a success: report it as a failure so the page keeps the ✗ lines up instead of reloading
    message = result.summary()
    if result.success and result.has_failures:
        message = f"Incomplete: run {'Start' if enable else 'Stop'} again.\n{message}"
    complete = result.success and not result.has_failures
    yield json.dumps({"done": True, "success": complete, "message": message}) + "\n"


def _action_start_competition(
    request: HttpRequest, config: CompetitionConfig, authentik_username: str
) -> StreamingHttpResponse:
    return StreamingHttpResponse(
        run_detached(_stream_competition_run(True, authentik_username)), content_type="application/x-ndjson"
    )


def _action_stop_competition(
    request: HttpRequest, config: CompetitionConfig, authentik_username: str
) -> StreamingHttpResponse:
    return StreamingHttpResponse(
        run_detached(_stream_competition_run(False, authentik_username)), content_type="application/x-ndjson"
    )


def _action_cleanup_competition(
    request: HttpRequest, config: CompetitionConfig, authentik_username: str
) -> JsonResponse:
    """Queue the competition cleanup for the bot, which also removes the Discord side."""
    if config.applications_enabled:
        return JsonResponse({"error": "Competition must be stopped before cleanup"}, status=400)

    DiscordTask.enqueue(CleanupCompetition(requested_by=authentik_username))
    return JsonResponse({"success": True, "message": "Cleanup queued. The bot reports progress in the ops channel."})


def _action_wipe_competition(request: HttpRequest, config: CompetitionConfig, authentik_username: str) -> JsonResponse:
    from core.competition_utils import wipe_competition_data

    if config.applications_enabled:
        return JsonResponse({"error": "Competition must be stopped before wiping"}, status=400)

    counts = wipe_competition_data()

    config.competition_start_time = None
    config.competition_end_time = None
    config.save()

    counters_reset = counts.pop("TeamTicketCounter")
    deleted_items = {k: v for k, v in counts.items() if v > 0}
    total_deleted = sum(counts.values())

    # The wipe keeps AuditLog, so the wipe itself is recorded alongside the history
    AuditLog.objects.create(
        action="competition_wiped",
        admin_user=authentik_username,
        target_entity="competition",
        target_id=0,
        details={
            "deleted_counts": deleted_items,
            "total_deleted": total_deleted,
            "ticket_counters_reset": counters_reset,
        },
    )

    summary_parts = [f"{v} {k}" for k, v in deleted_items.items()]
    summary = ", ".join(summary_parts) if summary_parts else "No data to delete"
    if counters_reset:
        summary += f"; {counters_reset} team ticket counters reset"

    return JsonResponse(
        {
            "success": True,
            "message": f"Competition wiped! Deleted: {summary}",
        }
    )


def _action_reset_passwords(request: HttpRequest, config: CompetitionConfig, authentik_username: str) -> JsonResponse:
    form = ResetPasswordsForm(request.POST)
    if not form.is_valid():
        error_msg = "; ".join(str(e) for errors in form.errors.values() for e in errors)
        return JsonResponse({"error": error_msg}, status=400)

    team_numbers = form.cleaned_data["team_numbers"] or active_team_numbers()

    password_list = []
    failed_resets = []
    sessions_failed = []

    for team_num in team_numbers:
        username = team_username(team_num)
        password, error, sessions_revoked = reset_team_credentials(team_num)
        if password:
            password_list.append((team_num, username, password))
            if not sessions_revoked:
                sessions_failed.append(username)
        else:
            failed_resets.append((username, error))

    csv_buffer = io.StringIO()
    writer = csv.writer(csv_buffer)
    writer.writerow(["Username", "Password"])
    for _team_num, username, password in password_list:
        writer.writerow([username, password])

    csv_content = csv_buffer.getvalue()

    AuditLog.objects.create(
        action="blueteam_passwords_reset",
        admin_user=authentik_username,
        target_entity="authentik_users",
        target_id=0,
        details={
            "total_users": len(team_numbers),
            "success_count": len(password_list),
            "failed_count": len(failed_resets),
            "sessions_failed": sessions_failed,
            "team_numbers": form.cleaned_data.get("team_numbers", "") or "all active",
        },
    )

    message = f"Reset {len(password_list)}/{len(team_numbers)} passwords"
    if sessions_failed:
        message += f"; could not revoke sessions for {', '.join(sessions_failed)}"
    return JsonResponse(
        {
            "success": True,
            "message": message,
            "csv": csv_content,
        }
    )


def _action_sync_quotient(request: HttpRequest, config: CompetitionConfig, authentik_username: str) -> JsonResponse:
    try:
        sync_quotient_metadata()
        AuditLog.objects.create(
            action="quotient_metadata_synced",
            admin_user=authentik_username,
            target_entity="quotient",
            target_id=0,
            details={},
        )
        return JsonResponse({"success": True, "message": "Quotient metadata synced"})
    except Exception as e:
        logger.error(f"Failed to sync Quotient metadata: {e}")
        return JsonResponse({"error": f"Sync failed: {e}"}, status=500)


_COMPETITION_ACTION_HANDLERS = {
    "set_max_members": _action_set_max_members,
    "add_app": _action_add_app,
    "remove_app": _action_remove_app,
    "set_start_time": _action_set_start_time,
    "set_end_time": _action_set_end_time,
    "set_schedule": _action_set_schedule,
    "start_competition": _action_start_competition,
    "stop_competition": _action_stop_competition,
    "cleanup_competition": _action_cleanup_competition,
    "wipe_competition": _action_wipe_competition,
    "reset_passwords": _action_reset_passwords,
    "sync_quotient": _action_sync_quotient,
    "readiness_check": action_readiness_check,
    "readiness_fix": action_readiness_fix,
}


@require_permission("admin", "gold_team")
def admin_competition(request: HttpRequest) -> HttpResponse:
    from team.models import DiscordLink, Team

    config = CompetitionConfig.get_config()

    active_teams = Team.objects.filter(is_active=True).count()
    total_teams = Team.objects.count()
    linked_users = DiscordLink.objects.filter(is_active=True, team__isnull=False).count()

    quotient_metadata = QuotientMetadataCache.objects.first()

    context = {
        "config": config,
        "active_teams": active_teams,
        "total_teams": total_teams,
        "linked_users": linked_users,
        "timezone_choices": TIMEZONE_CHOICES,
        "quotient_metadata": quotient_metadata,
        "show_ops_nav": True,
        "nav_active": "ops_admin",
    }

    return render(request, "admin/competition.html", context)


def admin_competition_apps(request: HttpRequest) -> JsonResponse:
    """Suggestions for the add-app field, fetched after the page renders (the call takes ~1s).

    Lists every app with a provider (see AuthentikManager.list_applications). A slug with no provider
    (e.g. a launcher-only app) won't appear but can still be typed; add validates it by direct lookup.
    """
    user = cast(User, request.user)
    if not _has_admin_or_gold_access(user):
        return JsonResponse({"error": "Access denied"}, status=403)
    return JsonResponse({"apps": AuthentikManager().list_applications()})


def admin_competition_action(request: HttpRequest) -> HttpResponseBase:
    if request.method != "POST":
        return HttpResponse("Method not allowed", status=405)

    user = cast(User, request.user)
    if not _has_admin_or_gold_access(user):
        return JsonResponse({"error": "Access denied"}, status=403)

    form = ActionForm(request.POST)
    if not form.is_valid():
        return JsonResponse({"error": "No action specified"}, status=400)
    action = form.cleaned_data["action"]
    handler = _COMPETITION_ACTION_HANDLERS.get(action)
    if not handler:
        return JsonResponse({"error": "Unknown action"}, status=400)

    config = CompetitionConfig.get_config()
    return handler(request, config, user.username)


@require_permission("admin", "gold_team")
def admin_competition_danger(request: HttpRequest) -> HttpResponse:
    """Danger zone page for destructive competition operations."""
    config = CompetitionConfig.get_config()
    return render(
        request,
        "admin/competition_danger.html",
        {
            "config": config,
            "show_ops_nav": True,
            "nav_active": "ops_admin",
        },
    )
