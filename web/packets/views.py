from collections.abc import Iterator
from typing import TypedDict, cast

from django.contrib import messages
from django.contrib.auth.models import User
from django.core.exceptions import BadRequest, PermissionDenied
from django.http import HttpRequest, HttpResponse, HttpResponseBase, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from core.auth_utils import get_user_team, require_permission
from core.stored_files import clean_name, file_response, stored_type
from core.utils import ndjson_done, run_detached
from team.models import Team

from .forms import PacketActionForm, PacketResendForm, PacketUploadForm
from .models import Packet, PacketDistribution
from .services import PacketDistributionService


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
        packets = Packet.objects.downloadable().order_by("-created_at")
        context: dict[str, object] = {"packets": packets, "is_gold_view": True}
        return render(request, "packets/team_packet.html", context)

    team = get_user_team(user)
    if not team:
        raise PermissionDenied("You are not assigned to a team.")

    context = {
        "distributions": PacketDistribution.objects.visible_to(team)
        .select_related("packet")
        .order_by("-packet__created_at"),
        "team": team,
    }

    return render(request, "packets/team_packet.html", context)


@require_GET
@require_permission("blue_team")
def download_packet(request: HttpRequest, packet_id: int) -> HttpResponse:
    """Download a packet file distributed to the caller's team (gold_team: any web-enabled packet)."""
    from core.auth_utils import has_permission

    user = cast(User, request.user)
    if has_permission(user, "gold_team"):
        packet = get_object_or_404(Packet.objects.downloadable(), id=packet_id)
    else:
        distribution = get_object_or_404(
            PacketDistribution.objects.visible_to(get_user_team(user)).select_related("packet"), packet_id=packet_id
        )
        distribution.record_download(user.username)
        packet = distribution.packet

    response = file_response(bytes(packet.file_data), packet.filename, inline=False)
    response["Content-Length"] = packet.file_size

    return response


@require_GET
@require_permission("gold_team")
def packets_list(request: HttpRequest) -> HttpResponse:
    """List all packets for GoldTeam administrators."""
    packets = list(Packet.objects.with_distribution_stats().order_by("-created_at"))

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
        "history": history,
        "teams": Team.objects.filter(is_active=True).order_by("team_number"),
    }

    return render(request, "packets/ops_packets_list.html", context)


@require_http_methods(["GET", "POST"])
@require_permission("gold_team")
def upload_packet(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        form = PacketUploadForm(request.POST, request.FILES)
        if not form.is_valid():
            error_msg = " ".join(str(e) for errors in form.errors.values() for e in errors)
            messages.error(request, error_msg)
            return render(request, "packets/ops_upload_packet.html")

        title = form.cleaned_data["title"]
        notes = form.cleaned_data["notes"]
        send_via_email = form.cleaned_data["send_via_email"]
        web_access_enabled = form.cleaned_data["web_access_enabled"]
        team_extras = form.cleaned_data["team_extras"]
        uploaded_file = form.cleaned_data["packet_file"]

        file_data = uploaded_file.read()
        filename = clean_name(uploaded_file.name, "unnamed")
        mime_type = stored_type(file_data, filename)

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

    return render(request, "packets/ops_upload_packet.html")


@require_GET
@require_permission("gold_team")
def packet_detail(request: HttpRequest, packet_id: int) -> HttpResponse:
    packet = get_object_or_404(Packet.objects.with_distribution_stats(), id=packet_id)

    distributions = packet.distributions.select_related("team", "team__school_info").order_by("team__team_number")
    teams = Team.objects.filter(is_active=True).order_by("team_number")

    context = {
        "packet": packet,
        "distributions": distributions,
        "stats": packet.get_distribution_stats(),
        "teams": teams,
    }

    return render(request, "packets/ops_packet_detail.html", context)


@require_POST
@require_permission("gold_team")
def packet_action(request: HttpRequest, packet_id: int) -> HttpResponseBase:
    """Dispatch packet actions (distribute, resend_failed, resend_pending, send_new_teams, cancel, test_email)."""
    packet = get_object_or_404(Packet, id=packet_id)

    form = PacketActionForm(request.POST)
    if not form.is_valid():
        error_msg = "; ".join(str(e) for errors in form.errors.values() for e in errors)
        return StreamingHttpResponse(
            iter([ndjson_done(error_msg, success=False)]),
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

    if action == "send_new_teams":
        return StreamingHttpResponse(
            run_detached(service.stream_send_to_new_teams(packet)),
            content_type="application/x-ndjson",
        )

    if action == "cancel":
        # A sent packet reached the teams; cancelling it would let the school list be cleared under it
        cancelled = Packet.objects.filter(pk=packet.pk, status="draft").update(
            status="cancelled", updated_at=timezone.now()
        )
        line = (
            ndjson_done("Packet cancelled")
            if cancelled
            else ndjson_done("Only a draft packet can be cancelled; this one has already been sent.", success=False)
        )
        return StreamingHttpResponse(iter([line]), content_type="application/x-ndjson")

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
                yield ndjson_done(msg)
            except Exception as e:
                yield ndjson_done(str(e), success=False)

        return StreamingHttpResponse(_stream_test(), content_type="application/x-ndjson")

    raise BadRequest("Unknown action")


@require_POST
@require_permission("gold_team")
def packet_resend_team(request: HttpRequest, packet_id: int, team_id: int) -> HttpResponse:
    """Resend a packet email to a specific team, saving any email changes."""
    packet = get_object_or_404(Packet, id=packet_id)
    team = get_object_or_404(Team, id=team_id)

    form = PacketResendForm(request.POST)
    if not form.is_valid():
        messages.error(request, f"Valid primary email is required for Team {team.team_number}.")
        return redirect("packet_detail", packet_id=packet_id)

    primary_email = form.cleaned_data["primary_email"]
    secondary_email = form.cleaned_data.get("secondary_email", "")

    recipients = ", ".join(filter(None, [primary_email, secondary_email]))
    try:
        if refusal := PacketDistributionService().resend_to_team(packet, team, primary_email, secondary_email):
            messages.error(request, refusal)
        else:
            messages.success(request, f"Email resent to Team {team.team_number} ({recipients}).")
    except Exception as e:
        messages.error(request, f"Failed to resend to Team {team.team_number}: {e}")

    return redirect("packet_detail", packet_id=packet_id)
