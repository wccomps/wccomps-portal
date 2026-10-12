import logging
from typing import cast

from django.contrib import messages
from django.contrib.auth.models import User
from django.core.files.uploadedfile import UploadedFile
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from registration.models import Season
from registration.services import (
    ImportBlockedError,
    SchoolTakenError,
    clear_school_list,
    import_problem,
    import_school_list,
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
from team.forms import CSVRowData, CSVUploadForm, ImportEventForm, parse_csv_file, validate_csv_data
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

    if request.method == "POST":
        deleted = clear_school_list()
        logger.info(f"Cleared school info by {request.user.username}")
        messages.success(request, f"Cleared {deleted} school info record(s).")
        return redirect("school_info")

    return render(
        request,
        "school_info_clear.html",
        {"count": count, "show_ops_nav": True},
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
                    "error": error_msg,
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
                request, "school_info_edit.html", {"team": team, "school_info": school_info, "error": str(taken)}
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
    csv_file: UploadedFile[bytes], event_id: int | None = None
) -> tuple[dict[str, object] | None, list[dict[str, object]]]:
    """Parse and validate a school-info CSV into (preview data, session rows; empty on errors)."""
    parse_result = parse_csv_file(csv_file)

    if parse_result["errors"]:
        return {
            "errors": parse_result["errors"],
            "warnings": parse_result["warnings"],
            "rows": [],
        }, []

    validation_result = validate_csv_data(parse_result["rows"], event_id)

    preview_data: dict[str, object] = {
        "errors": validation_result["errors"],
        "warnings": parse_result["warnings"] + validation_result["warnings"],
        "teams_to_create": validation_result["teams_to_create"],
        "can_import": not validation_result["errors"],
    }

    session_rows: list[dict[str, object]] = []
    if preview_data["can_import"]:
        session_rows = [
            {
                "team_number": row["team_number"],
                "school_name": row["school_name"],
                "contact_email": row["contact_email"],
                "secondary_email": row.get("secondary_email", ""),
                "notes": row.get("notes", ""),
                "returning_events": row.get("returning_events"),
            }
            for row in validation_result["teams_to_create"]
        ]

    return preview_data, session_rows


def _import_error(*errors: str) -> dict[str, object]:
    return {"errors": list(errors), "warnings": [], "can_import": False}


def _upload_again(*problems: str) -> tuple[dict[str, object], None]:
    return _import_error(*problems, "Upload the file again."), None


def _apply_school_info_import(
    import_data: dict[str, object], authentik_username: str
) -> tuple[dict[str, object] | None, dict[str, int] | None]:
    """Re-check the teams and the event fields, then import the list (which re-checks the event under its lock):
    (preview_data, None) on errors, else (None, counts)."""
    # Copies: the rows gain a Team object, which must not leak into the JSON session
    teams_to_create: list[CSVRowData] = [cast(CSVRowData, dict(row)) for row in import_data["teams_to_create"]]  # type: ignore[attr-defined]

    team_numbers = [row["team_number"] for row in teams_to_create]
    teams_by_number: dict[int, Team] = {t.team_number: t for t in Team.objects.filter(team_number__in=team_numbers)}

    missing = [
        f"Team {row['team_number']} no longer exists"
        for row in teams_to_create
        if row["team_number"] not in teams_by_number
    ]
    if missing:
        return _upload_again(*missing)
    for row in teams_to_create:
        row["_team"] = teams_by_number[row["team_number"]]

    event_fields = import_data.get("event")
    if not isinstance(event_fields, dict):
        return _upload_again("The uploaded file is no longer available.")
    season = Season.objects.active()
    if season is None:
        return _upload_again("No season is active.")
    event_form = ImportEventForm(event_fields, season=season)
    if not event_form.is_valid():
        return _upload_again(*(str(e) for e in event_form.non_field_errors()))
    try:
        counts = import_school_list(event_form.event_to_import, teams_to_create, authentik_username)
    except ImportBlockedError as blocked:
        return _import_error(str(blocked)), None
    return None, counts


@require_permission("gold_team")
def school_info_import(request: HttpRequest) -> HttpResponse:
    user = cast(User, request.user)
    authentik_username = user.username

    permissions = get_permissions_context(user)
    form = CSVUploadForm()
    preview_data: dict[str, object] | None = None
    import_results: dict[str, int] | None = None
    error = ""

    season = Season.objects.active()
    if season is None:
        error = "Activate a season first (Registration, Seasons): imported schools join an event of the active season."

    if request.method == "POST":
        if "upload" in request.POST:
            form = CSVUploadForm(request.POST, request.FILES)
            event_form = ImportEventForm(request.POST, season=season) if season else None
            if form.is_valid() and event_form is not None and event_form.is_valid():
                event = event_form.event_to_import
                if problem := import_problem(event):
                    error = problem
                else:
                    csv_file = cast(UploadedFile[bytes], request.FILES["csv_file"])
                    preview_data, session_rows = _parse_school_info_csv(csv_file, event.pk)
                    if preview_data is not None and session_rows:
                        request.session["csv_import_data"] = {
                            "teams_to_create": session_rows,
                            "event": {name: event_form.data.get(name, "") for name in event_form.fields},
                        }
                        preview_data["event_label"] = str(event) if event.pk else f"New: {event.name}"
            elif event_form is not None and not event_form.is_valid():
                error = " ".join(str(e) for e in event_form.non_field_errors())

        elif "confirm" in request.POST:
            import_data = request.session.get("csv_import_data")
            if import_data:
                preview_data, import_results = _apply_school_info_import(import_data, authentik_username)
                if import_results is not None:
                    del request.session["csv_import_data"]
            else:
                error = "The uploaded file is no longer available. Upload it again to import."
        else:
            error = "The form was submitted without an action. Upload the file again."

    return render(
        request,
        "school_info_import.html",
        {
            "authentik_username": authentik_username,
            "form": form,
            "open_events": season.events.filter(is_finalized=False) if season else [],
            "preview_data": preview_data,
            "import_results": import_results,
            "error": error,
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
