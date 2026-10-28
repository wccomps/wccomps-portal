import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from functools import partial
from typing import cast

from django.contrib.auth.models import User
from django.core.exceptions import BadRequest
from django.db import transaction
from django.http import HttpRequest, HttpResponse, HttpResponseBase, JsonResponse, StreamingHttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.http import require_POST
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
from registration.stage import EventStage, event_stage
from scoring.models import QuotientMetadataCache
from scoring.quotient_sync import sync_quotient_metadata

from core.admin_views.event_panel import event_facts, event_panel
from core.admin_views.places import (
    Anchor,
    Section,
    SetupSection,
    ids,
    section_partial,
    setup_url,
)
from core.admin_views.readiness import action_readiness_check, action_readiness_fix
from core.authentik_manager import AuthentikAPIError, AuthentikManager, reason_for
from core.authentik_utils import credentials_csv, reset_teams_credentials
from core.competition_utils import WipedData, competition_data_counts
from core.discord_tasks import LogToChannel
from core.errors import json_endpoint
from core.forms import (
    ActionForm,
    AppSlugForm,
    CancelEventForm,
    CloseOutForm,
    ResetPasswordsForm,
    SetMaxMembersForm,
    StartMessageForm,
    TypedConfirmForm,
)
from core.models import AuditLog, CompetitionConfig, DiscordTask
from core.services.competition import CompetitionRunResult, last_start_accounts, run_competition
from core.utils import ndjson_done, run_detached
from core.utils import ndjson_progress as _progress
from team.models import active_team_numbers

from ..auth_utils import require_permission
from ..utils import UnknownTimezoneError, parse_datetime_to_utc

logger = logging.getLogger(__name__)

TIMEZONE_CHOICES = [
    ("America/Los_Angeles", "Pacific Time (PT)"),
    ("America/Denver", "Mountain Time (MT)"),
    ("America/Chicago", "Central Time (CT)"),
    ("America/New_York", "Eastern Time (ET)"),
    ("UTC", "UTC"),
]


def _action_set_max_members(request: HttpRequest, config: CompetitionConfig, authentik_username: str) -> JsonResponse:
    form = SetMaxMembersForm(request.POST)
    if not form.is_valid():
        raise BadRequest("Max members must be 1-20")

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
        raise BadRequest(form.errors["start_message"][0])

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
        raise BadRequest("Please provide an app slug")
    slug = form.cleaned_data["app_slug"]
    # Start and Stop's lookup: the picker's provider list misses apps without a provider
    if add:
        try:
            app = AuthentikManager().get_application_by_slug(slug)
        except AuthentikAPIError as e:
            return JsonResponse(
                {"success": False, "message": f"Could not ask Authentik about '{slug}': {reason_for(e)}"}
            )
        if app is None:
            raise BadRequest(f"No Authentik application with the slug '{slug}'")

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


def _action_set_schedule(request: HttpRequest, config: CompetitionConfig, authentik_username: str) -> JsonResponse:
    """Handle set_schedule action — set start and/or end time in one request."""
    from core.forms import SetScheduleForm

    form = SetScheduleForm(request.POST)
    if not form.is_valid():
        first_error = next(iter(form.errors.values()), ["Invalid schedule data"])
        raise BadRequest(str(first_error[0]))

    start_dt = form.cleaned_data["start_datetime"]
    start_tz = form.cleaned_data.get("start_timezone") or "America/Los_Angeles"
    end_dt = form.cleaned_data["end_datetime"]
    end_tz = form.cleaned_data.get("end_timezone") or "America/Los_Angeles"

    try:
        start_time = parse_datetime_to_utc(start_dt, start_tz) if start_dt else None
        end_time = parse_datetime_to_utc(end_dt, end_tz) if end_dt else None
    except UnknownTimezoneError as e:
        raise BadRequest(str(e)) from e
    except ValueError:
        raise BadRequest("Invalid datetime format") from None
    if error := config.schedule_error(start_time, end_time):
        raise BadRequest(error)

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
    yield ndjson_done(message, success=complete)


def _toggle_competition(
    request: HttpRequest, config: CompetitionConfig, authentik_username: str, *, enable: bool
) -> StreamingHttpResponse:
    return StreamingHttpResponse(
        run_detached(_stream_competition_run(enable, authentik_username)), content_type="application/x-ndjson"
    )


def _action_clear_leftover_data(
    request: HttpRequest, config: CompetitionConfig, authentik_username: str
) -> JsonResponse:
    _require_typed_confirmation(request)
    try:
        wiped = clear_leftover_data(authentik_username)
    except CloseOutError as e:
        raise BadRequest(str(e)) from e
    return JsonResponse(
        {
            "success": True,
            "message": f"Leftover data cleared. Deleted: {_wiped_summary(wiped)}. Next: {event_stage().next_step}",
        }
    )


def _action_cancel_event(request: HttpRequest, config: CompetitionConfig, authentik_username: str) -> JsonResponse:
    form = CancelEventForm(request.POST)
    if not form.is_valid():
        raise BadRequest(form.first_error())
    try:
        event = cancel_event(form.cleaned_data["open_event"], authentik_username)
    except CloseOutError as e:
        raise BadRequest(str(e)) from e
    return JsonResponse(
        {"success": True, "message": f"{event.name} cancelled. The school list stays. Next: {event_stage().next_step}"}
    )


def _require_typed_confirmation(request: HttpRequest) -> None:
    form = TypedConfirmForm(request.POST)
    if not form.is_valid():
        raise BadRequest(form.first_error())


def _wiped_summary(wiped: WipedData) -> str:
    summary = ", ".join(f"{v} {k}" for k, v in wiped.deleted.items() if v > 0) or "No data to delete"
    if wiped.ticket_counters_reset:
        summary += f"; {wiped.ticket_counters_reset} team ticket counters reset"
    return summary


def _action_reset_passwords(request: HttpRequest, config: CompetitionConfig, authentik_username: str) -> JsonResponse:
    form = ResetPasswordsForm(request.POST)
    if not form.is_valid():
        error_msg = "; ".join(str(e) for errors in form.errors.values() for e in errors)
        raise BadRequest(error_msg)

    team_numbers = form.cleaned_data["team_numbers"] or active_team_numbers()
    result = reset_teams_credentials(team_numbers, actor=authentik_username)
    password_list = result.password_list
    csv_content = (
        credentials_csv((username, password) for _num, username, password in password_list) if password_list else ""
    )

    return JsonResponse({"success": not result.failed_resets, "message": result.summary(), "csv": csv_content})


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


# The Before you close out section shows it when the on-demand check fails
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


_COMPETITION_ACTION_HANDLERS: dict[str, Callable[[HttpRequest, CompetitionConfig, str], HttpResponseBase]] = {
    "close_out_event": _action_close_out_event,
    "close_out_check": _action_close_out_check,
    "set_max_members": _action_set_max_members,
    "set_start_message": _action_set_start_message,
    "add_app": partial(_edit_controlled_app, add=True),
    "remove_app": partial(_edit_controlled_app, add=False),
    "set_schedule": _action_set_schedule,
    "start_competition": partial(_toggle_competition, enable=True),
    "stop_competition": partial(_toggle_competition, enable=False),
    "clear_leftover_data": _action_clear_leftover_data,
    "cancel_event": _action_cancel_event,
    "reset_passwords": _action_reset_passwords,
    "sync_quotient": _action_sync_quotient,
    "readiness_check": action_readiness_check,
    "readiness_fix": action_readiness_fix,
}


@dataclass(frozen=True)
class LiveNumbers:
    """What the competition page shows while the competition runs: a few counts from a fixed number of queries."""

    unresolved_tickets: int  # Open or claimed
    # Team accounts the last start turned on, and how many it tried; None when no start is recorded
    accounts: tuple[int, int] | None


def _live_numbers() -> LiveNumbers:
    from ticketing.models import Ticket

    unresolved = Ticket.objects.filter(status__in=(Ticket.STATUS_OPEN, Ticket.STATUS_CLAIMED)).count()
    return LiveNumbers(unresolved, last_start_accounts())


def _live_context(stage: EventStage) -> dict[str, object]:
    return {
        "live": _live_numbers(),
        "quotient_metadata": QuotientMetadataCache.objects.first(),
        "scoring_engine_url": setup_url(SetupSection.SCORING_ENGINE),
    }


def _close_out_context(stage: EventStage) -> dict[str, object]:
    refusal = close_out_problem(stage, check_schools=False)
    active_season = Season.objects.active()
    return {
        "close_out_check_failed": CLOSE_OUT_CHECK_FAILED,
        # Close Out records scores with no event in the active season; the Seasons page makes one active
        "needs_active_season": stage.event is None and active_season is None,
        "active_season": active_season,
        "can_close_out": not refusal,
        # Shown when the section offers no button
        "close_out_refusal": refusal,
        "event_type_choices": Event.EVENT_TYPE_CHOICES,
    }


# The data each competition page section needs beyond the Event and Steps sections
SECTION_CONTEXT: dict[Section, Callable[[EventStage], dict[str, object]]] = {
    Section.LIVE: _live_context,
    Section.CLOSE_OUT: _close_out_context,
}


@require_permission("admin", "gold_team")
def admin_competition(request: HttpRequest) -> HttpResponse:
    stage = event_stage()
    panel = event_panel(event_facts(stage))
    can_clear = not clear_leftovers_problem(stage)
    context: dict[str, object] = {
        "panel": panel,
        "anchors": ids(Anchor),
        "can_cancel": not cancel_event_problem(stage),
        "can_clear": can_clear,
        "leftovers": _wiped_summary(competition_data_counts()) if can_clear else "",
    }
    if (section := panel.section) is not None:
        context["section_partial"] = section_partial(section)
        if section in SECTION_CONTEXT:
            context |= SECTION_CONTEXT[section](stage)
    return render(request, "admin/competition.html", context)


@require_permission("admin", "gold_team")
def admin_competition_setup(request: HttpRequest) -> HttpResponse:
    """Saves go through admin_competition_action, like the competition page's."""
    return render(
        request,
        "admin/competition_setup.html",
        {
            "config": CompetitionConfig.get_config(),
            "timezone_choices": TIMEZONE_CHOICES,
            "quotient_metadata": QuotientMetadataCache.objects.first(),
            "start_message_max_length": StartMessageForm.MAX_LENGTH,
            "sections": ids(SetupSection),
        },
    )


@json_endpoint
@require_permission("admin", "gold_team")
def admin_competition_apps(request: HttpRequest) -> JsonResponse:
    """Suggestions for the add-app field, fetched after the page renders (the call takes ~1s).

    Lists every app with a provider (see AuthentikManager.list_applications). A slug with no provider
    (e.g. a launcher-only app) won't appear but can still be typed; add validates it by direct lookup.
    Authentik's failure comes back as 200: the request was fine, and the page says why the list is empty.
    """
    try:
        return JsonResponse({"apps": AuthentikManager().list_applications()})
    except AuthentikAPIError as e:
        return JsonResponse({"error": reason_for(e)})


@json_endpoint
@require_POST
@require_permission("admin", "gold_team")
def admin_competition_action(request: HttpRequest) -> HttpResponseBase:
    user = cast(User, request.user)
    form = ActionForm(request.POST)
    if not form.is_valid():
        raise BadRequest("No action specified")
    action = form.cleaned_data["action"]
    handler = _COMPETITION_ACTION_HANDLERS.get(action)
    if not handler:
        raise BadRequest("Unknown action")

    config = CompetitionConfig.get_config()
    return handler(request, config, user.username)
