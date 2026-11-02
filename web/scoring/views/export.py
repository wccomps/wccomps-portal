import io
import logging
import zipfile
from collections.abc import Iterator

import weasyprint
from django.conf import settings
from django.contrib import messages
from django.core import mail
from django.http import HttpRequest, HttpResponse, StreamingHttpResponse
from django.shortcuts import redirect, render
from django.template.loader import render_to_string
from django.utils import timezone
from django.views.decorators.http import require_POST
from registration.close_out_checks import scorecards_fingerprint
from registration.models import Event
from registration.stage import ScorecardsEmailed

from core.auth_utils import require_permission
from core.utils import ndjson_done, ndjson_progress, run_detached

from .. import export
from ..calculator import Standing, compute_standings, get_leaderboard, get_standing
from ..forms import ScorecardEmailForm
from ..scorecard import ScorecardPeerContext, build_peer_context, build_scorecard_context

logger = logging.getLogger(__name__)


@require_permission("gold_team", error_message="Only Gold Team members can access this")
def export_index(request: HttpRequest) -> HttpResponse:
    return render(request, "scoring/export_index.html")


@require_permission("gold_team", error_message="Only Gold Team members can access this")
def export_dataset(request: HttpRequest, dataset: str) -> HttpResponse:
    return export.export_dataset(dataset, request.GET.get("format", "csv").lower())


@require_permission("gold_team", error_message="Only Gold Team members can access this")
def export_all(request: HttpRequest) -> HttpResponse:
    return export.export_all_zip()


@require_permission("gold_team", error_message="Only Gold Team members can access this")
def export_scorecards(request: HttpRequest) -> HttpResponse:
    standings = compute_standings()
    peer_context = build_peer_context(standings)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for score in get_leaderboard(standings):
            zf.writestr(
                f"team-{score.team.team_number:02d}-scorecard.pdf",
                _generate_team_pdf(score, standings, request, peer_context=peer_context),
            )

    timestamp = timezone.now().strftime("%Y%m%d-%H%M%S")
    response = HttpResponse(buf.getvalue(), content_type="application/zip")
    response["Content-Disposition"] = f'attachment; filename="scorecards-{timestamp}.zip"'
    return response


def _send_scorecard_email(
    recipients: list[str], context: dict[str, object], team_number: int, pdf_bytes: bytes
) -> bool:
    """Send a scorecard email using Django's email backend."""
    try:
        subject = render_to_string("emails/scorecard_subject.txt", context).strip()
        text_content = render_to_string("emails/scorecard.txt", context)
        html_content = render_to_string("emails/scorecard.html", context)

        email = mail.EmailMultiAlternatives(
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
    team = score.team
    school_name = score.school_name or team.team_name

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
        "point_adjustments": score.point_adjustments,
        "total_score": score.total_score,
        "rank": score.rank,
        "total_teams": total_teams,
        "scorecard_attached": True,
        "custom_message": custom_message,
    }


def _deny_external_urls(url: str, *_args: object, **_kwargs: object) -> dict[str, object]:
    """Block all external and local-file URL fetches during WeasyPrint PDF rendering."""
    raise ValueError(f"External URL fetching is disabled in scorecard PDFs: {url}")


def render_scorecard_pdf(context: dict[str, object], request: HttpRequest | None = None) -> bytes:
    """Render a scorecard context to PDF bytes with external URL fetching disabled."""
    html_string = render_to_string("scoring/scorecard_print.html", context, request=request)
    pdf_bytes: bytes = weasyprint.HTML(string=html_string, url_fetcher=_deny_external_urls).write_pdf()
    return pdf_bytes


def _generate_team_pdf(
    score: Standing,
    standings: list[Standing],
    request: HttpRequest,
    peer_context: ScorecardPeerContext | None = None,
) -> bytes:
    return render_scorecard_pdf(build_scorecard_context(score, standings, peer_context=peer_context), request=request)


def _stream_email_scorecards(request: HttpRequest, custom_message: str = "") -> Iterator[str]:
    """Generator that sends scorecard emails and yields NDJSON progress."""
    standings = compute_standings()
    peer_context = build_peer_context(standings)
    scores = get_leaderboard(standings)
    total_teams = len(scores)

    sendable = [(score.team, score, score.school_emails) for score in scores if score.school_emails]
    # What these scorecards say, from the standings the emails are built from, so Close Out Event can tell
    # whether the scores changed since
    event = Event.objects.open()
    fingerprint = scorecards_fingerprint(event, standings)

    total = len(sendable)
    if total == 0:
        yield ndjson_done("No teams with email addresses", success=True)
        return

    sent = 0
    failed = 0
    for i, (team, score, emails) in enumerate(sendable, 1):
        email_ctx = _build_email_context(score, total_teams, custom_message=custom_message)
        pdf_bytes = _generate_team_pdf(score, standings, request, peer_context=peer_context)
        success = _send_scorecard_email(emails, email_ctx, team.team_number, pdf_bytes)

        if success:
            sent += 1
            yield ndjson_progress(f"Sent to Team {team.team_number}", i, total)
        else:
            failed += 1
            logger.error("Failed to email scorecard to Team %d", team.team_number)
            yield ndjson_progress(f"Failed Team {team.team_number}", i, total, ok=False)

    if sent:
        ScorecardsEmailed(event.pk if event else None, sent, failed, fingerprint).record(request.user.get_username())
    message = f"Sent {sent}, failed {failed}" if failed else f"Emailed scorecards to {sent} teams"
    yield ndjson_done(message, success=failed == 0)


@require_permission("gold_team", error_message="Only Gold Team members can email scorecards")
def email_scorecards(request: HttpRequest) -> HttpResponse:
    """Email scorecards confirmation page (GET only)."""
    scores = get_leaderboard()

    if not scores:
        messages.error(request, "No ranked teams yet.")
        return redirect("leaderboard_page")

    team_rows = [
        {
            "team": score.team,
            "score": score,
            "school_name": score.school_name,
            "emails": score.school_emails,
            "has_email": bool(score.school_emails),
        }
        for score in scores
    ]

    teams_with_email = sum(1 for r in team_rows if r["has_email"])
    teams_without_email = sum(1 for r in team_rows if not r["has_email"])

    return render(
        request,
        "scoring/email_scorecards_confirm.html",
        {
            "team_rows": team_rows,
            "teams_with_email": teams_with_email,
            "teams_without_email": teams_without_email,
        },
    )


@require_POST
@require_permission("gold_team", error_message="Only Gold Team members can email scorecards")
def stream_email_scorecards(request: HttpRequest) -> StreamingHttpResponse:
    """Stream scorecard email sending progress as NDJSON."""
    form = ScorecardEmailForm(request.POST)
    custom_message = form.get_custom_message()
    return StreamingHttpResponse(
        run_detached(_stream_email_scorecards(request, custom_message=custom_message)),
        content_type="application/x-ndjson",
    )


@require_permission("gold_team", error_message="Only Gold Team members can email scorecards")
def email_scorecard(request: HttpRequest, team_number: int) -> HttpResponse:
    """Email scorecard to a single team. GET shows confirmation, POST sends."""
    standings = compute_standings()
    score = get_standing(team_number, standings)
    team = score.team
    emails = score.school_emails
    school_name = score.school_name

    if not emails:
        messages.error(request, f"Team {team_number} has no school info / contact email.")
        return redirect("leaderboard_scorecard", team_number=team_number)

    if request.method == "POST":
        form = ScorecardEmailForm(request.POST)
        custom_message = form.get_custom_message()
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
            "school_name": school_name,
            "emails": emails,
        },
    )
