import csv
import io
import json
import mimetypes
import re
from collections.abc import Iterator
from typing import TypedDict, cast

from django.contrib import messages
from django.contrib.auth.models import User
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseBase, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from core.auth_utils import get_user_team, require_permission
from core.utils import run_detached
from team.models import Team

from .forms import PacketActionForm, PacketResendForm, PacketUploadForm
from .models import Packet, PacketDistribution
from .services import EARLIER_EVENT_PACKET, PacketDistributionService, packet_is_from_open_event


class PacketWithStats(TypedDict):
    packet: Packet
    stats: dict[str, int]


@require_GET
@require_permission("blue_team")
def team_packet(request: HttpRequest) -> HttpResponse:
    """List all available packets for the current team (or all packets for gold_team)."""
    from core.auth_utils import has_permission

    user = cast(User, request.user)
    is_gold = has_permission(user, "gold_team")

    if is_gold:
        packets = Packet.objects.filter(status__in=["distributing", "completed"], web_access_enabled=True).order_by(
            "-created_at"
        )
        context: dict[str, object] = {"packets": packets, "is_gold_view": True}
        return render(request, "packets/team_packet.html", context)

    team = get_user_team(user)
    if not team:
        messages.error(request, "You are not assigned to a team.")
        return redirect("/")

    from team.forms import active_event

    # Only the open event's packets: team numbers are reshuffled, so an earlier event's packet went to another school
    event = active_event()
    distributions = (
        PacketDistribution.objects.filter(team=team, web_access_enabled=True, packet__event=event)
        .select_related("packet")
        .order_by("-packet__created_at")
        if event
        else PacketDistribution.objects.none()
    )

    available_distributions = [
        dist
        for dist in distributions
        if dist.packet.status in ["distributing", "completed"] and dist.packet.web_access_enabled
    ]

    context = {
        "distributions": available_distributions,
        "team": team,
    }

    return render(request, "packets/team_packet.html", context)


@require_GET
@require_permission("blue_team")
def download_packet(request: HttpRequest, packet_id: int) -> HttpResponse:
    """Download a packet file distributed to the caller's team (gold_team: any web-enabled packet)."""
    from core.auth_utils import has_permission

    user = cast(User, request.user)
    available = {"status__in": ["distributing", "completed"], "web_access_enabled": True}
    if has_permission(user, "gold_team"):
        packet = get_object_or_404(Packet, id=packet_id, **available)
    else:
        from team.forms import active_event

        event = active_event()
        if event is None:
            raise Http404("No open event")
        distribution = get_object_or_404(
            PacketDistribution.objects.select_related("packet"),
            packet_id=packet_id,
            team=get_user_team(user),
            web_access_enabled=True,
            packet__event=event,
            **{f"packet__{k}": v for k, v in available.items()},
        )
        distribution.record_download(user.username)
        packet = distribution.packet

    response = HttpResponse(bytes(packet.file_data), content_type=packet.mime_type)
    response["Content-Disposition"] = f'attachment; filename="{packet.filename}"'
    response["Content-Length"] = packet.file_size

    return response


@require_GET
@require_permission("gold_team")
def packets_list(request: HttpRequest) -> HttpResponse:
    """List all packets for GoldTeam administrators."""
    packets = list(Packet.objects.all().order_by("-created_at"))

    # Active = draft or distributing (there's typically only one)
    active_packet: Packet | None = None
    active_stats: dict[str, int] = {}
    history: list[PacketWithStats] = []

    for packet in packets:
        stats = packet.get_distribution_stats()
        if not active_packet and packet.status in ("draft", "distributing"):
            active_packet = packet
            active_stats = stats
        else:
            history.append({"packet": packet, "stats": stats})

    context: dict[str, object] = {
        "active_packet": active_packet,
        "active_stats": active_stats,
        "active_distributions": (
            active_packet.distributions.select_related("team", "team__school_info").order_by("team__team_number")
            if active_packet
            else []
        ),
        "history": history,
        "teams": Team.objects.filter(is_active=True).order_by("team_number"),
        "nav_active": "ops_admin",
    }

    return render(request, "packets/ops_packets_list.html", context)


def _parse_team_extras_csv(csv_text: str) -> dict[str, dict[str, str]]:
    """Parse per-team CSV data into a dict keyed by team number.

    Expects a CSV with a 'team' column (e.g. 'Team01', 'Team 3', '5').
    Returns dict like {"1": {"api_key": "sk-...", ...}, "2": {...}}.
    """
    reader = csv.DictReader(io.StringIO(csv_text.strip()))
    if not reader.fieldnames:
        return {}

    team_col = None
    for col in reader.fieldnames:
        if "team" in col.lower():
            team_col = col
            break

    if not team_col:
        return {}

    result: dict[str, dict[str, str]] = {}
    other_cols = [c for c in reader.fieldnames if c != team_col]

    for row in reader:
        raw_team = row.get(team_col, "").strip()
        # Extract team number: "Team01" -> 1, "Team 3" -> 3, "5" -> 5
        digits = re.sub(r"[^\d]", "", raw_team)
        if not digits:
            continue
        team_num = str(int(digits))
        result[team_num] = {col: row.get(col, "").strip() for col in other_cols}

    return result


@require_http_methods(["GET", "POST"])
@require_permission("gold_team")
def upload_packet(request: HttpRequest) -> HttpResponse:
    form_context = {"nav_active": "packets"}

    if request.method == "POST":
        form = PacketUploadForm(request.POST, request.FILES)
        if not form.is_valid():
            error_msg = " ".join(str(e) for errors in form.errors.values() for e in errors)
            messages.error(request, error_msg)
            return render(request, "packets/ops_upload_packet.html", form_context)

        title = form.cleaned_data["title"]
        notes = form.cleaned_data["notes"]
        send_via_email = form.cleaned_data["send_via_email"]
        web_access_enabled = form.cleaned_data["web_access_enabled"]
        team_extras_csv = form.cleaned_data["team_extras"]
        uploaded_file = form.cleaned_data["packet_file"]

        file_data = uploaded_file.read()
        filename = (uploaded_file.name or "unnamed")[:255]
        mime_type = uploaded_file.content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"

        team_extras: dict[str, dict[str, str]] = {}
        if team_extras_csv:
            team_extras = _parse_team_extras_csv(team_extras_csv)
            if not team_extras:
                messages.error(request, "Could not parse per-team data. Ensure CSV has a 'team' column.")
                return render(request, "packets/ops_upload_packet.html", form_context)

        Packet.objects.create(
            title=title,
            file_data=file_data,
            filename=filename,
            mime_type=mime_type,
            file_size=len(file_data),
            status="draft",
            send_via_email=send_via_email,
            web_access_enabled=web_access_enabled,
            uploaded_by=request.user.username,
            notes=notes,
            team_extras=team_extras,
        )

        messages.success(
            request,
            f"Packet '{title}' uploaded successfully. Use 'Distribute' to send to all teams.",
        )
        return redirect("packets_list")

    return render(request, "packets/ops_upload_packet.html", form_context)


@require_GET
@require_permission("gold_team")
def packet_detail(request: HttpRequest, packet_id: int) -> HttpResponse:
    packet = get_object_or_404(Packet, id=packet_id)

    distributions = packet.distributions.select_related("team", "team__school_info").order_by("team__team_number")
    teams = Team.objects.filter(is_active=True).order_by("team_number")

    context = {
        "packet": packet,
        "distributions": distributions,
        "stats": packet.get_distribution_stats(),
        "teams": teams,
        "nav_active": "ops_admin",
    }

    return render(request, "packets/ops_packet_detail.html", context)


@require_POST
def packet_action(request: HttpRequest, packet_id: int) -> HttpResponseBase:
    """Dispatch packet actions (distribute, resend_failed, cancel, test_email)."""
    from core.auth_utils import has_permission

    if not has_permission(cast(User, request.user), "gold_team"):
        return HttpResponse("Access denied", status=403)

    packet = get_object_or_404(Packet, id=packet_id)

    form = PacketActionForm(request.POST)
    if not form.is_valid():
        error_msg = "; ".join(str(e) for errors in form.errors.values() for e in errors)
        return StreamingHttpResponse(
            iter([json.dumps({"done": True, "success": False, "message": error_msg}) + "\n"]),
            content_type="application/x-ndjson",
        )

    action = form.cleaned_data["action"]
    service = PacketDistributionService()

    if action == "distribute":
        return StreamingHttpResponse(
            run_detached(service.stream_distribute_packet(packet)),
            content_type="application/x-ndjson",
        )

    if action == "resend_failed":
        return StreamingHttpResponse(
            run_detached(service.stream_resend_failed(packet)),
            content_type="application/x-ndjson",
        )

    if action == "resend_pending":
        return StreamingHttpResponse(
            run_detached(service.stream_retry_pending(packet)),
            content_type="application/x-ndjson",
        )

    if action == "cancel":
        packet.status = "cancelled"
        packet.save(update_fields=["status", "updated_at"])
        return StreamingHttpResponse(
            iter([json.dumps({"done": True, "success": True, "message": "Packet cancelled"}) + "\n"]),
            content_type="application/x-ndjson",
        )

    if action == "test_email":
        from core.utils import ndjson_progress

        email = form.cleaned_data["email"]
        team_id = form.cleaned_data["team_id"]
        team = get_object_or_404(Team, id=team_id)

        def _stream_test() -> Iterator[str]:
            yield ndjson_progress(f"Sending test email to {email}...", 0, 1)
            try:
                service.send_test_packet_email(packet, team, email)
                yield ndjson_progress("Sent", 1, 1)
                msg = f"Test email sent to {email} as Team {team.team_number}"
                yield json.dumps({"done": True, "success": True, "message": msg}) + "\n"
            except Exception as e:
                yield json.dumps({"done": True, "success": False, "message": str(e)}) + "\n"

        return StreamingHttpResponse(_stream_test(), content_type="application/x-ndjson")

    return HttpResponse("Unknown action", status=400)


@require_POST
@require_permission("gold_team")
def packet_resend_team(request: HttpRequest, packet_id: int, team_id: int) -> HttpResponse:
    """Resend a packet email to a specific team, saving any email changes."""
    from team.models import SchoolInfo

    packet = get_object_or_404(Packet, id=packet_id)
    team = get_object_or_404(Team, id=team_id)

    if not packet_is_from_open_event(packet):
        messages.error(request, EARLIER_EVENT_PACKET)
        return redirect("packet_detail", packet_id=packet_id)

    dist = PacketDistribution.objects.filter(packet=packet, team=team).first()
    if not dist:
        messages.error(request, f"No distribution record for Team {team.team_number}.")
        return redirect("packet_detail", packet_id=packet_id)

    form = PacketResendForm(request.POST)
    if not form.is_valid():
        messages.error(request, f"Valid primary email is required for Team {team.team_number}.")
        return redirect("packet_detail", packet_id=packet_id)

    primary_email = form.cleaned_data["primary_email"]
    secondary_email = form.cleaned_data.get("secondary_email", "")

    school_info, _ = SchoolInfo.objects.get_or_create(
        team=team, defaults={"school_name": f"Team {team.team_number}", "contact_email": primary_email}
    )
    school_info.contact_email = primary_email
    school_info.secondary_email = secondary_email
    school_info.save(update_fields=["contact_email", "secondary_email"])

    recipients = [primary_email]
    if secondary_email:
        recipients.append(secondary_email)

    try:
        dist.email_status = "pending"
        dist.email_error_message = ""
        dist.save(update_fields=["email_status", "email_error_message"])

        service = PacketDistributionService()
        service.send_packet_email(dist, override_emails=recipients)
        messages.success(request, f"Email resent to Team {team.team_number} ({', '.join(recipients)}).")
    except Exception as e:
        messages.error(request, f"Failed to resend to Team {team.team_number}: {e}")

    return redirect("packet_detail", packet_id=packet_id)
