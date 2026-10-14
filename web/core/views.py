import logging
import secrets
from contextlib import suppress
from dataclasses import asdict
from typing import cast

from django.contrib import messages
from django.contrib.auth.models import User
from django.core.files.uploadedfile import UploadedFile
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from registration.forms import ShownSchoolForm
from registration.models import Event, Season
from registration.services import (
    ImportBlockedError,
    NewEvent,
    PacketsSentError,
    SchoolChangedError,
    SchoolListRow,
    SchoolTakenError,
    clear_school_list,
    remove_team_school,
    set_team_school,
)

from core.services.linking import (
    LinkResult,
    enforce_account_link_policy,
    execute_link,
    finalize_link,
    store_discord_id_in_authentik,
    validate_link_token,
)
from team.forms import (
    IMPORT_MODES,
    CSVUploadForm,
    ImportConfirmForm,
    ImportEventForm,
    ImportMode,
    parse_csv_file,
)
from team.models import LinkToken, SchoolInfo, Team

from .auth_utils import (
    get_authentik_groups,
    get_authentik_id,
    get_permissions_context,
    get_role_based_landing_url,
    require_permission,
    team_for_groups,
)
from .forms import LinkConfirmForm, SchoolInfoEditForm

logger = logging.getLogger(__name__)


def home(request: HttpRequest) -> HttpResponse:
    """Home page - redirect to appropriate dashboard based on user role."""
    user = cast(User, request.user)
    groups = get_authentik_groups(user)

    url = get_role_based_landing_url(groups)
    if url != "/":
        return redirect(url)

    # Redirecting to a page the user can't open would bounce back here forever.
    return render(
        request,
        "error.html",
        {
            "error": "No access",
            "message": "Your account has no portal role yet. Ask an organizer to add you to the right group.",
        },
        status=403,
    )


def link_initiate(request: HttpRequest) -> HttpResponse:
    token = request.GET.get("token")

    if not token:
        return HttpResponse("Missing token parameter", status=400)

    try:
        link_token = LinkToken.objects.get(token=token, used=False)
    except LinkToken.DoesNotExist:
        return render(
            request,
            "link_error.html",
            {
                "error": "Invalid or expired token",
                "message": "This link has expired or is invalid. Please use /link in Discord to generate a new one.",
            },
        )

    if link_token.is_expired():
        return render(
            request,
            "link_error.html",
            {
                "error": "Token expired",
                "message": (
                    "This link has expired (15 minute limit). Please use /link in Discord to generate a new one."
                ),
            },
        )

    # The callback only completes in a browser that came through here (core.services.linking)
    request.session["pending_link_token"] = link_token.token
    request.session["pending_link_discord_id"] = link_token.discord_id

    # Pass token through OAuth redirect via next parameter
    from urllib.parse import quote

    next_url = quote(f"/auth/link-callback?token={link_token.token}", safe="")
    return redirect(f"/auth/login/?next={next_url}")


def link_callback(request: HttpRequest) -> HttpResponse:
    """Handle OAuth callback after Authentik authentication."""
    # Drop messages queued before the login round trip; this page shows its own
    list(messages.get_messages(request))

    user = cast(User, request.user)
    try:
        authentik_username = user.username
        groups = get_authentik_groups(user)
        authentik_user_id = get_authentik_id(user)
    except Exception as e:
        logger.error(f"link_callback: Error getting Authentik data: {e}", exc_info=True)
        raise

    if not authentik_user_id:
        return render(
            request,
            "link_error.html",
            {
                "error": "Authentication error",
                "message": "Could not retrieve your Authentik account information.",
            },
        )

    def _render_error(result: LinkResult) -> HttpResponse:
        return render(request, cast(str, result.error_template), result.error_context)

    # Validate token (URL on GET, confirmation form on POST) against the one this browser started with
    if request.method == "POST":
        confirm_form = LinkConfirmForm(request.POST)
        url_token = confirm_form.cleaned_data["token"] if confirm_form.is_valid() else None
    else:
        url_token = request.GET.get("token")
    session_token = request.session.get("pending_link_token")
    token_result = validate_link_token(url_token, session_token, authentik_username)
    if isinstance(token_result, LinkResult):
        return _render_error(token_result)
    link_token = token_result

    discord_id = link_token.discord_id
    discord_username = link_token.discord_username

    team = team_for_groups(groups)

    # Nothing is linked until the user confirms which Discord account they are linking (POST + CSRF).
    # Opening someone else's /link URL would otherwise silently hand them this account's roles.
    if request.method != "POST":
        return render(
            request,
            "link_confirm.html",
            {
                "token": link_token.token,
                "discord_username": link_token.discord_username,
                "authentik_username": authentik_username,
                "team_name": f"Team {team.team_number}" if team else None,
                "is_team_account": team is not None,
            },
        )

    # Enforce one-to-one link policy for non-team accounts
    policy_error = enforce_account_link_policy(user, discord_id, discord_username, authentik_username, team)
    if policy_error:
        return _render_error(policy_error)

    if not team:
        store_discord_id_in_authentik(authentik_username, discord_id, authentik_user_id)

    link_error = execute_link(discord_id, discord_username, user, team)
    if link_error:
        return _render_error(link_error)

    finalize_link(link_token, discord_id, discord_username, authentik_username, team)

    request.session.pop("pending_link_token", None)
    request.session.pop("pending_link_discord_id", None)

    return render(
        request,
        "link_success.html",
        {
            "team_name": f"Team {team.team_number}" if team else None,
            "team_number": team.team_number if team else None,
            "discord_username": discord_username,
            "authentik_username": authentik_username,
            "is_team_account": team is not None,
        },
    )


@require_permission("gold_team")
def school_info(request: HttpRequest) -> HttpResponse:
    teams = Team.objects.filter(is_active=True).select_related("school_info__school").order_by("team_number")

    teams_with_info = []
    for team in teams:
        try:
            school_info = team.school_info
        except SchoolInfo.DoesNotExist:
            school_info = None

        teams_with_info.append({"team": team, "school_info": school_info})

    return render(
        request,
        "school_info.html",
        {
            "teams": teams_with_info,
            "show_ops_nav": True,
        },
    )


@require_permission("gold_team")
def school_info_export(request: HttpRequest) -> HttpResponse:
    import csv

    records = SchoolInfo.objects.select_related("team", "school").order_by("team__team_number")

    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="school_info.csv"'

    writer = csv.writer(response)
    writer.writerow(["team_number", "team_name", "school_name", "contact_email", "secondary_email", "notes"])

    for si in records:
        writer.writerow(
            [
                si.team.team_number,
                si.team.team_name,
                si.school_name,
                si.contact_email,
                si.secondary_email or "",
                si.notes or "",
            ]
        )

    return response


@require_permission("gold_team")
def school_info_clear(request: HttpRequest) -> HttpResponse:
    count = SchoolInfo.objects.count()

    error = ""
    if request.method == "POST":
        try:
            deleted = clear_school_list()
        except PacketsSentError as sent:
            error = str(sent)
        else:
            logger.info(f"Cleared school info by {request.user.username}")
            messages.success(request, f"Cleared {deleted} school info record(s).")
            return redirect("school_info")

    return render(
        request,
        "school_info_clear.html",
        {"count": count, "error": error, "show_ops_nav": True},
    )


@require_permission("gold_team")
def school_info_remove(request: HttpRequest, team_number: int) -> HttpResponse:
    school_info = get_object_or_404(SchoolInfo.objects.select_related("team", "school"), team__team_number=team_number)
    error = ""
    if request.method == "POST":
        shown = ShownSchoolForm(request.POST)
        try:
            remove_team_school(school_info.team, expected_school_id=shown.expected_school_id())
        except (PacketsSentError, SchoolChangedError) as refused:
            error = str(refused)
        else:
            logger.info(f"Removed Team {team_number}'s school info by {request.user.username}")
            messages.success(request, f"Removed {school_info.school_name} from Team {team_number}.")
            return redirect("school_info")

    return render(
        request,
        "school_info_remove.html",
        {"school_info": school_info, "error": error, "show_ops_nav": True},
    )


@require_permission("gold_team")
def school_info_edit(request: HttpRequest, team_number: int) -> HttpResponse:
    user = cast(User, request.user)
    authentik_username = user.username

    try:
        team = Team.objects.get(team_number=team_number, is_active=True)
    except Team.DoesNotExist:
        return render(
            request,
            "error.html",
            {
                "error": "Team not found",
                "message": f"Team {team_number} does not exist.",
            },
        )

    try:
        school_info = team.school_info
    except SchoolInfo.DoesNotExist:
        school_info = None

    if request.method == "POST":
        form = SchoolInfoEditForm(request.POST)
        if not form.is_valid():
            error_msg = " ".join(str(e) for errors in form.errors.values() for e in errors)
            return render(
                request,
                "school_info_edit.html",
                {
                    "team": team,
                    "school_info": school_info,
                    "page_error": error_msg,
                },
            )

        school_name = form.cleaned_data["school_name"]
        contact_email = form.cleaned_data["contact_email"]
        secondary_email = form.cleaned_data.get("secondary_email", "")
        notes = form.cleaned_data.get("notes", "")

        try:
            school_info = set_team_school(
                team,
                school_name,
                contact_email=contact_email,
                secondary_email=secondary_email,
                notes=notes,
                updated_by=authentik_username,
            )
        except SchoolTakenError as taken:
            return render(
                request, "school_info_edit.html", {"team": team, "school_info": school_info, "page_error": str(taken)}
            )

        logger.info(f"School info updated for Team {team_number} by {authentik_username}")

        return redirect("school_info")

    return render(
        request,
        "school_info_edit.html",
        {
            "team": team,
            "school_info": school_info,
            "show_ops_nav": True,
        },
    )


def _parse_school_info_csv(
    csv_file: UploadedFile[bytes], event: Event | NewEvent, mode: ImportMode
) -> tuple[dict[str, object] | None, list[dict[str, object]]]:
    """Parse and validate a school-info CSV for the import mode into (preview data, session rows; empty on errors)."""
    parse_result = parse_csv_file(csv_file)

    if parse_result["errors"]:
        return {
            "errors": parse_result["errors"],
            "warnings": parse_result["warnings"],
            "rows": [],
        }, []

    validation_result = mode.validate(parse_result["rows"], event)

    preview_data: dict[str, object] = {
        "summary": "" if validation_result["errors"] else mode.preview(event, validation_result["teams_to_create"]),
        "errors": validation_result["errors"],
        "warnings": parse_result["warnings"] + validation_result["warnings"],
        "teams_to_create": validation_result["teams_to_create"],
        "can_import": not validation_result["errors"],
    }

    session_rows: list[dict[str, object]] = []
    if preview_data["can_import"]:
        session_rows = [
            asdict(
                SchoolListRow(
                    team_number=row["team_number"],
                    school_name=row["school_name"],
                    contact_email=row["contact_email"],
                    secondary_email=row.get("secondary_email", ""),
                    notes=row.get("notes", ""),
                )
            )
            for row in validation_result["teams_to_create"]
        ]

    return preview_data, session_rows


# The session's import previews by token, so a confirm runs the preview on its page and not another tab's
PREVIEWS_KEY = "school_list_previews"
PREVIEWS_KEPT = 5


def _store_preview(request: HttpRequest, import_data: dict[str, object]) -> str:
    """Keep the preview's rows and event fields for its confirm, dropping the oldest beyond PREVIEWS_KEPT; its token."""
    token = secrets.token_urlsafe(16)
    previews = request.session.get(PREVIEWS_KEY)
    previews = dict(previews) if isinstance(previews, dict) else {}
    previews[token] = import_data
    request.session[PREVIEWS_KEY] = dict(list(previews.items())[-PREVIEWS_KEPT:])
    return token


def _import_error(*errors: str) -> dict[str, object]:
    return {"errors": list(errors), "warnings": [], "can_import": False}


def _upload_again(*problems: str) -> tuple[dict[str, object], None]:
    return _import_error(*problems, "Upload the file again."), None


def _apply_school_info_import(
    import_data: dict[str, object], authentik_username: str
) -> tuple[dict[str, object] | None, dict[str, object] | None]:
    """Re-check the event fields, then apply the stored mode (its service re-checks the event and the teams under the
    school list lock): (preview_data, None) on errors, else (None, results)."""
    stored_rows = import_data.get("teams_to_create")
    event_fields = import_data.get("event")
    rows = None
    if isinstance(stored_rows, list):
        with suppress(TypeError):
            rows = [SchoolListRow(**row) for row in stored_rows]
    if rows is None or not isinstance(event_fields, dict):
        return _upload_again("The uploaded file is no longer available.")
    event_form = ImportEventForm(event_fields, season=Season.objects.active())
    if not event_form.is_valid():
        return _upload_again(*(str(e) for e in event_form.non_field_errors()))
    event = event_form.event_to_import
    mode = IMPORT_MODES[event_form.import_mode]
    try:
        counts = mode.apply(event, rows, authentik_username)
    except ImportBlockedError as blocked:
        return _import_error(str(blocked)), None
    lines = mode.result(event, counts)
    if isinstance(event, NewEvent) and event.season is not None:
        lines.append(f"Created the {event.season} season and made it the active one.")
    return None, {"lines": lines, **counts}


@require_permission("gold_team")
def school_info_import(request: HttpRequest) -> HttpResponse:
    user = cast(User, request.user)
    authentik_username = user.username

    permissions = get_permissions_context(user)
    form = CSVUploadForm()
    preview_data: dict[str, object] | None = None
    import_results: dict[str, object] | None = None
    error = ""

    season = Season.objects.active()

    if request.method == "POST":
        if "upload" in request.POST:
            form = CSVUploadForm(request.POST, request.FILES)
            event_form = ImportEventForm(request.POST, season=season)
            if not event_form.is_valid():
                error = " ".join(str(e) for e in event_form.non_field_errors())
            elif form.is_valid():
                event = event_form.event_to_import
                mode = IMPORT_MODES[event_form.import_mode]
                if problem := mode.problem(event):
                    error = problem
                else:
                    csv_file = cast(UploadedFile[bytes], request.FILES["csv_file"])
                    preview_data, session_rows = _parse_school_info_csv(csv_file, event, mode)
                    if preview_data is not None and session_rows:
                        preview_data["token"] = _store_preview(
                            request,
                            {
                                "teams_to_create": session_rows,
                                "event": {name: event_form.data.get(name, "") for name in event_form.fields},
                            },
                        )
                        preview_data["event_label"] = str(event)
                        preview_data["mode_label"] = mode.label.format(event=event)

        elif "confirm" in request.POST:
            stored = request.session.get(PREVIEWS_KEY)
            previews: dict[str, object] = stored if isinstance(stored, dict) else {}
            confirm_form = ImportConfirmForm(request.POST)
            token = confirm_form.cleaned_data["preview"] if confirm_form.is_valid() else ""
            import_data = previews.get(token)
            if isinstance(import_data, dict):
                preview_data, import_results = _apply_school_info_import(import_data, authentik_username)
                if import_results is not None:
                    del previews[token]
                    request.session.modified = True
            else:
                error = "The uploaded file is no longer available. Upload it again to import."
        else:
            error = "The form was submitted without an action. Upload the file again."

    open_event = Event.objects.open() or "the open event"
    return render(
        request,
        "school_info_import.html",
        {
            "authentik_username": authentik_username,
            "form": form,
            "active_season": season,
            "open_events": season.events.filter(is_finalized=False) if season else [],
            "import_modes": [(key, mode.label.format(event=open_event)) for key, mode in IMPORT_MODES.items()],
            "preview_data": preview_data,
            "import_results": import_results,
            "page_error": error,
            "show_ops_nav": True,
            **permissions,
        },
    )


def livez(request: HttpRequest) -> HttpResponse:
    """Liveness: the process is serving requests. Deliberately skips the database (see health_check)."""
    return HttpResponse("ok", content_type="text/plain")


def health_check(request: HttpRequest) -> HttpResponse:
    """Health check endpoint for monitoring - tests database connectivity and model queries."""
    from django.apps import apps
    from django.db import connection

    errors = []

    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception as e:
        errors.append(f"Database connection: {e!s}")

    for model_class in apps.get_app_config("core").get_models():
        try:
            model_class._default_manager.exists()
        except Exception as e:
            errors.append(f"{model_class.__name__}: {str(e)[:100]}")

    if errors:
        return JsonResponse({"status": "unhealthy", "errors": errors}, status=503)
    return JsonResponse({"status": "healthy"})
