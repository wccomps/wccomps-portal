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
from django.urls import reverse
from registration.close_out_checks import close_out_warnings
from registration.models import Event, Season
from registration.services import (
    CloseOutError,
    NewEvent,
    TeamFix,
    cancel_event,
    cancel_event_problem,
    clear_leftover_data,
    clear_leftovers_problem,
    close_out_event,
    close_out_fixes,
    close_out_problem,
)
from registration.stage import Stage, event_stage
from scoring.models import QuotientMetadataCache
from scoring.quotient_sync import sync_quotient_metadata

from core.admin_views.event_panel import RARE_ACTIONS, STEPS, action_url, event_facts, event_panel
from core.admin_views.readiness import action_readiness_check, action_readiness_fix
from core.authentik_manager import AuthentikManager
from core.authentik_utils import reset_team_credentials
from core.competition_utils import WipedData, competition_data_counts
from core.discord_tasks import LogToChannel
from core.forms import (
    ActionForm,
    AppSlugForm,
    CancelEventForm,
    CloseOutForm,
    ResetPasswordsForm,
    SetMaxMembersForm,
    SetTimeForm,
    StartMessageForm,
    TypedConfirmForm,
)
from core.models import AuditLog, CompetitionConfig, DiscordTask
from core.services.competition import CompetitionRunResult, run_competition
from core.utils import ndjson_done, run_detached
from core.utils import ndjson_progress as _progress
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


def _action_set_start_message(request: HttpRequest, config: CompetitionConfig, authentik_username: str) -> JsonResponse:
    form = StartMessageForm(request.POST)
    if not form.is_valid():
        return JsonResponse({"error": form.errors["start_message"][0]}, status=400)

    message = form.cleaned_data["start_message"]
    CompetitionConfig.objects.filter(pk=config.pk).update(start_message=message)

    # The message usually carries credentials, so the audit entry records only its length.
    AuditLog.objects.create(
        action="competition_start_message_updated",
        admin_user=authentik_username,
        target_entity="competition_config",
        target_id=config.pk,
        details={"length": len(message)},
    )

    return JsonResponse(
        {"success": True, "message": "Start message saved" if message.strip() else "Start message cleared"}
    )


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


def _action_clear_leftover_data(
    request: HttpRequest, config: CompetitionConfig, authentik_username: str
) -> JsonResponse:
    if error := _typed_confirmation_error(request):
        return error
    try:
        wiped = clear_leftover_data(authentik_username)
    except CloseOutError as e:
        return JsonResponse({"error": str(e)}, status=400)
    return JsonResponse(
        {
            "success": True,
            "message": f"Leftover data cleared. Deleted: {_wiped_summary(wiped)}. Next: {event_stage().next_step}",
        }
    )


def _action_cancel_event(request: HttpRequest, config: CompetitionConfig, authentik_username: str) -> JsonResponse:
    form = CancelEventForm(request.POST)
    if not form.is_valid():
        return JsonResponse({"error": form.first_error()}, status=400)
    try:
        event = cancel_event(form.cleaned_data["open_event"], authentik_username)
    except CloseOutError as e:
        return JsonResponse({"error": str(e)}, status=400)
    return JsonResponse(
        {"success": True, "message": f"{event.name} cancelled. The school list stays. Next: {event_stage().next_step}"}
    )


def _typed_confirmation_error(request: HttpRequest) -> JsonResponse | None:
    form = TypedConfirmForm(request.POST)
    if form.is_valid():
        return None
    return JsonResponse({"error": form.errors["confirm"][0]}, status=400)


def _wiped_summary(wiped: WipedData) -> str:
    summary = ", ".join(f"{v} {k}" for k, v in wiped.deleted.items() if v > 0) or "No data to delete"
    if wiped.ticket_counters_reset:
        summary += f"; {wiped.ticket_counters_reset} team ticket counters reset"
    return summary


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
        sync_quotient_metadata(cast(User, request.user))
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


def _stream_close_out(target: Event | NewEvent, authentik_username: str) -> Iterator[str]:
    yield _progress("Closing out: freezing the results, then clearing the live data", 1, 1)
    try:
        outcome = close_out_event(target, authentik_username)
    except CloseOutError as e:
        yield ndjson_done(str(e), success=False)
        return
    yield ndjson_done(
        f"Closed out {outcome.event.name}: froze {outcome.results} school result(s) and the export archive, "
        f"deleted {_wiped_summary(outcome.wiped)}, and queued the Discord cleanup (the bot reports it in the ops "
        f"channel). Next: {event_stage().next_step}"
    )


def _action_close_out_check(request: HttpRequest, config: CompetitionConfig, authentik_username: str) -> JsonResponse:
    """What would make Close Out refuse, and the unfinished work it would freeze, fetched once the page shows the
    section: the scorecard check builds every school's scorecard, too slow for the page load, and a failure here
    must not break the page that holds Start and Stop."""
    try:
        stage = event_stage()
        warnings = [{"text": w.text, "url": reverse(w.url_name)} for w in close_out_warnings(stage.event)]
        problem = close_out_problem(stage)
        fixes = [link for fix in close_out_fixes(stage) for link in _fix_links(fix)] if problem else []
    except Exception:
        logger.exception("Close-out check failed")
        return JsonResponse({"error": CLOSE_OUT_CHECK_FAILED}, status=500)
    return JsonResponse({"success": True, "problem": problem, "fixes": fixes, "warnings": warnings})


# The panel and the Close Out section show it when the on-demand check fails
CLOSE_OUT_CHECK_FAILED = (
    "Couldn't check unfinished work. Closing out still works: check grading, tickets and the scorecard emails by hand."
)


def _fix_links(fix: TeamFix) -> list[dict[str, str]]:
    """Links to where the team's Close Out problem is fixed."""
    places = [
        (fix.activate, "Activate Team", "admin_team_detail"),
        (fix.enter_school, "enter its school", "school_info_edit"),
        (fix.remove_school, "remove its school", "school_info_remove"),
    ]
    return [
        {"text": f"Team {fix.team_number}: {text}", "url": reverse(url_name, args=[fix.team_number])}
        for offered, text, url_name in places
        if offered
    ]


def _action_close_out_event(
    request: HttpRequest, config: CompetitionConfig, authentik_username: str
) -> StreamingHttpResponse:
    form = CloseOutForm(request.POST)
    if not form.is_valid():
        return StreamingHttpResponse(
            iter([ndjson_done(form.first_error(), success=False)]), content_type="application/x-ndjson"
        )
    return StreamingHttpResponse(
        run_detached(_stream_close_out(form.target(), authentik_username)), content_type="application/x-ndjson"
    )


_COMPETITION_ACTION_HANDLERS = {
    "close_out_event": _action_close_out_event,
    "close_out_check": _action_close_out_check,
    "set_max_members": _action_set_max_members,
    "set_start_message": _action_set_start_message,
    "add_app": _action_add_app,
    "remove_app": _action_remove_app,
    "set_start_time": _action_set_start_time,
    "set_end_time": _action_set_end_time,
    "set_schedule": _action_set_schedule,
    "start_competition": _action_start_competition,
    "stop_competition": _action_stop_competition,
    "clear_leftover_data": _action_clear_leftover_data,
    "cancel_event": _action_cancel_event,
    "reset_passwords": _action_reset_passwords,
    "sync_quotient": _action_sync_quotient,
    "readiness_check": action_readiness_check,
    "readiness_fix": action_readiness_fix,
}


@require_permission("admin", "gold_team")
def admin_competition(request: HttpRequest) -> HttpResponse:

    from team.models import DiscordLink, Team

    config = CompetitionConfig.get_config()
    stage = event_stage()

    active_teams = Team.objects.filter(is_active=True).count()
    total_teams = Team.objects.count()
    linked_users = DiscordLink.objects.filter(is_active=True, team__isnull=False).count()

    quotient_metadata = QuotientMetadataCache.objects.first()

    close_out_refusal = close_out_problem(stage, check_schools=False)
    active_season = Season.objects.active()
    context = {
        "panel": event_panel(event_facts(stage)),
        "close_out_check_failed": CLOSE_OUT_CHECK_FAILED,
        # Close Out records scores with no event in the active season; the Seasons page makes one active
        "needs_active_season": stage.event is None and active_season is None,
        "active_season": active_season,
        "open_event": stage.event,
        "can_close_out": not close_out_refusal,
        # Shown where the module offers no button for a reason the operator can act on
        "close_out_refusal": close_out_refusal if stage.stage in (Stage.READY, Stage.PLAYED, Stage.UNRECORDED) else "",
        "event_type_choices": Event.EVENT_TYPE_CHOICES,
        "config": config,
        "active_teams": active_teams,
        "total_teams": total_teams,
        "linked_users": linked_users,
        "timezone_choices": TIMEZONE_CHOICES,
        "quotient_metadata": quotient_metadata,
        "start_message_max_length": StartMessageForm.MAX_LENGTH,
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
    stage = event_stage()
    can_clear = not clear_leftovers_problem(stage)
    return render(
        request,
        "admin/competition_danger.html",
        {
            "config": config,
            "stage": stage,
            "leftovers": _wiped_summary(competition_data_counts()) if can_clear else "",
            "can_cancel": not cancel_event_problem(stage),
            "show_ops_nav": True,
            "nav_active": "ops_admin",
        },
    )


def guide_partial(key: str) -> str:
    """The guide's section of prose for a step or a rare action, named after its key."""
    return f"admin/guide/{key}.html"


@require_permission("admin", "gold_team")
def admin_competition_guide(request: HttpRequest) -> HttpResponse:
    """Running an event, step by step, in plain words: the panel's steps, each with its own section of prose."""
    rare = [
        {"title": title, "key": action.value, "url": action_url(action), "partial": guide_partial(action)}
        for action, title in RARE_ACTIONS
    ]
    steps = [{"step": step, "partial": guide_partial(step.key)} for step in STEPS]
    return render(
        request,
        "admin/competition_guide.html",
        {"steps": steps, "rare": rare, "show_ops_nav": True, "nav_active": "ops_admin"},
    )
