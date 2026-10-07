from collections.abc import Iterator

from django.http import HttpRequest, HttpResponse, StreamingHttpResponse
from django.views.decorators.http import require_POST

from core.auth_utils import require_permission
from core.utils import ndjson_progress, run_detached

from ..calculator import Standing, compute_standings, get_leaderboard, get_standing
from ..forms import ScorecardEmailForm
from .leaderboard import build_scorecard_context


@require_permission("gold_team", error_message="Only Gold Team members can access this")
def export_index(request: HttpRequest) -> HttpResponse:
    from django.shortcuts import render

    return render(request, "scoring/export_index.html")


@require_permission("gold_team", error_message="Only Gold Team members can access this")
def export_dataset(request: HttpRequest, dataset: str) -> HttpResponse:
    from .. import export

    return export.export_dataset(dataset, request.GET.get("format", "csv").lower())


@require_permission("gold_team", error_message="Only Gold Team members can access this")
def export_all(request: HttpRequest) -> HttpResponse:
    """Export all scoring data as a zip file (admin only)."""
    from ..export import export_all_zip

    return export_all_zip()


@require_permission("gold_team", error_message="Only Gold Team members can access this")
def export_scorecards(request: HttpRequest) -> HttpResponse:
    """Export all team scorecards as a zip of PDFs."""
    import io
    import zipfile

    from django.utils import timezone

    standings = compute_standings()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for score in get_leaderboard(standings):
            zf.writestr(
                f"team-{score.team.team_number:02d}-scorecard.pdf", _generate_team_pdf(score, standings, request)
            )

    timestamp = timezone.now().strftime("%Y%m%d-%H%M%S")
    response = HttpResponse(buf.getvalue(), content_type="application/zip")
    response["Content-Disposition"] = f'attachment; filename="scorecards-{timestamp}.zip"'
    return response


def _send_scorecard_email(
    recipients: list[str], context: dict[str, object], team_number: int, pdf_bytes: bytes
) -> bool:
    """Send a scorecard email using Django's email backend."""
    import logging

    from django.conf import settings
    from django.core.mail import EmailMultiAlternatives
    from django.template.loader import render_to_string

    logger = logging.getLogger(__name__)

    try:
        subject = render_to_string("emails/scorecard_subject.txt", context).strip()
        text_content = render_to_string("emails/scorecard.txt", context)
        html_content = render_to_string("emails/scorecard.html", context)

        email = EmailMultiAlternatives(
            subject=subject,
            body=text_content,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=recipients,
        )
        email.attach_alternative(html_content, "text/html")
        email.attach(f"team-{team_number:02d}-scorecard.pdf", pdf_bytes, "application/pdf")
        email.send(fail_silently=False)
        return True
    except Exception:
        logger.exception("Failed to send scorecard email to Team %d", team_number)
        return False


def _build_email_context(score: Standing, total_teams: int, custom_message: str = "") -> dict[str, object]:
    """Build email template context for a team's scorecard."""
    from django.utils import timezone

    from team.models import SchoolInfo

    team = score.team

    try:
        school_info = team.school_info
        school_name = school_info.school_name
    except SchoolInfo.DoesNotExist:
        school_name = team.team_name

    return {
        "event_date": timezone.now(),
        "school_name": school_name,
        "team_number": team.team_number,
        "service_points": score.service_points,
        "inject_points": score.inject_points,
        "orange_points": score.orange_points,
        "red_deductions": score.red_deductions,
        "incident_recovery_points": score.incident_recovery_points,
        "sla_penalties": score.sla_penalties,
        "total_score": score.total_score,
        "rank": score.rank,
        "total_teams": total_teams,
        "scorecard_attached": True,
        "custom_message": custom_message,
    }


def _generate_team_pdf(score: Standing, standings: list[Standing], request: HttpRequest) -> bytes:
    import weasyprint
    from django.template.loader import render_to_string

    context = build_scorecard_context(score, standings)
    html_string = render_to_string("scoring/scorecard_print.html", context, request=request)
    pdf_bytes: bytes = weasyprint.HTML(string=html_string).write_pdf()
    return pdf_bytes


def _stream_email_scorecards(request: HttpRequest, custom_message: str = "") -> Iterator[str]:
    """Generator that sends scorecard emails and yields NDJSON progress."""
    import json
    import logging

    from team.models import SchoolInfo

    logger = logging.getLogger(__name__)

    standings = compute_standings()
    scores = get_leaderboard(standings)
    total_teams = len(scores)

    sendable = []
    for score in scores:
        team = score.team
        try:
            school_info = team.school_info
            emails = [school_info.contact_email]
            if school_info.secondary_email:
                emails.append(school_info.secondary_email)
            sendable.append((team, score, emails))
        except SchoolInfo.DoesNotExist:
            continue

    total = len(sendable)
    if total == 0:
        yield json.dumps({"done": True, "success": True, "message": "No teams with email addresses"}) + "\n"
        return

    sent = 0
    failed = 0
    for i, (team, score, emails) in enumerate(sendable, 1):
        email_ctx = _build_email_context(score, total_teams, custom_message=custom_message)
        pdf_bytes = _generate_team_pdf(score, standings, request)
        success = _send_scorecard_email(emails, email_ctx, team.team_number, pdf_bytes)

        if success:
            sent += 1
            yield ndjson_progress(f"Sent to Team {team.team_number}", i, total)
        else:
            failed += 1
            logger.error("Failed to email scorecard to Team %d", team.team_number)
            yield ndjson_progress(f"Failed Team {team.team_number}", i, total, ok=False)

    message = f"Sent {sent}, failed {failed}" if failed else f"Emailed scorecards to {sent} teams"
    yield json.dumps({"done": True, "success": failed == 0, "message": message}) + "\n"


@require_permission("gold_team", error_message="Only Gold Team members can email scorecards")
def email_scorecards(request: HttpRequest) -> HttpResponse:
    """Email scorecards confirmation page (GET only)."""
    from django.contrib import messages
    from django.shortcuts import redirect, render

    from team.models import SchoolInfo

    scores = get_leaderboard()

    if not scores:
        messages.error(request, "No ranked teams yet.")
        return redirect("leaderboard_page")

    team_rows = []
    for score in scores:
        team = score.team
        try:
            school_info = team.school_info
            emails_list = [school_info.contact_email]
            if school_info.secondary_email:
                emails_list.append(school_info.secondary_email)
            team_rows.append(
                {
                    "team": team,
                    "score": score,
                    "school_name": school_info.school_name,
                    "emails": emails_list,
                    "has_email": True,
                }
            )
        except SchoolInfo.DoesNotExist:
            team_rows.append(
                {
                    "team": team,
                    "score": score,
                    "school_name": "",
                    "emails": [],
                    "has_email": False,
                }
            )

    teams_with_email = sum(1 for r in team_rows if r["has_email"])
    teams_without_email = sum(1 for r in team_rows if not r["has_email"])

    return render(
        request,
        "scoring/email_scorecards_confirm.html",
        {
            "team_rows": team_rows,
            "teams_with_email": teams_with_email,
            "teams_without_email": teams_without_email,
            "total_teams": len(scores),
        },
    )


@require_POST
@require_permission("gold_team", error_message="Only Gold Team members can email scorecards")
def stream_email_scorecards(request: HttpRequest) -> StreamingHttpResponse:
    """Stream scorecard email sending progress as NDJSON."""
    form = ScorecardEmailForm(request.POST)
    custom_message = form.cleaned_data["custom_message"].strip() if form.is_valid() else ""
    return StreamingHttpResponse(
        run_detached(_stream_email_scorecards(request, custom_message=custom_message)),
        content_type="application/x-ndjson",
    )


@require_permission("gold_team", error_message="Only Gold Team members can email scorecards")
def email_scorecard(request: HttpRequest, team_number: int) -> HttpResponse:
    """Email scorecard to a single team. GET shows confirmation, POST sends."""
    from django.contrib import messages
    from django.shortcuts import redirect, render

    from team.models import SchoolInfo

    standings = compute_standings()
    score = get_standing(team_number, standings)
    team = score.team

    try:
        school_info = team.school_info
        emails = [school_info.contact_email]
        if school_info.secondary_email:
            emails.append(school_info.secondary_email)
        school_name = school_info.school_name
    except SchoolInfo.DoesNotExist:
        messages.error(request, f"Team {team_number} has no school info / contact email.")
        return redirect("leaderboard_scorecard", team_number=team_number)

    if request.method == "POST":
        form = ScorecardEmailForm(request.POST)
        custom_message = form.cleaned_data["custom_message"].strip() if form.is_valid() else ""
        email_ctx = _build_email_context(score, len(get_leaderboard(standings)), custom_message=custom_message)
        pdf_bytes = _generate_team_pdf(score, standings, request)

        success = _send_scorecard_email(emails, email_ctx, team_number, pdf_bytes)

        if success:
            messages.success(request, f"Scorecard emailed to {', '.join(emails)}.")
        else:
            messages.error(request, f"Failed to email scorecard to Team {team_number}.")

        return redirect("leaderboard_scorecard", team_number=team_number)

    return render(
        request,
        "scoring/email_scorecard_confirm.html",
        {
            "team": team,
            "score": score,
            "school_name": school_name,
            "emails": emails,
        },
    )
