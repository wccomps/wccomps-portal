import hashlib
import logging
from collections.abc import Iterable
from itertools import takewhile
from typing import cast

from django.contrib import messages
from django.contrib.auth.models import User
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from core.auth_utils import get_user_team, has_permission, require_permission, team_record_or_404
from core.discord_tasks import PostComment
from core.models import DiscordTask
from ticketing.access import TICKET_STAFF, ticket_actions
from ticketing.forms import TicketCommentForm
from ticketing.models import (
    BOX_RESET_CATEGORY_ID,
    Ticket,
    TicketAttachment,
    TicketCategory,
    TicketComment,
    TicketHistory,
)

logger = logging.getLogger(__name__)


def list_version(rows: Iterable[TicketComment | TicketHistory]) -> str:
    """A short marker of the rows a polled list shows. The page and each poll carry it, and a poll whose marker
    still matches answers 204, so htmx swaps nothing and the aria-live region stays quiet."""
    return hashlib.sha256(",".join(str(row.pk) for row in rows).encode()).hexdigest()[:16]


def _ticketing_staff_usernames() -> list[str]:
    """Usernames that can work tickets, for the assign box's suggestions."""
    from core.models import UserGroups
    from core.permission_constants import PERMISSION_MAP

    staff = UserGroups.objects.granting(PERMISSION_MAP[TICKET_STAFF])
    return list(staff.order_by("user__username").values_list("user__username", flat=True))


def _visible_ticket(user: User, ticket_number: str) -> Ticket:
    """The ticket as the person may see it: ticketing staff see every ticket, a team its own (another team's is a
    404, as a missing one is)."""
    return team_record_or_404(
        Ticket.objects.select_related("team", "category"), user, staff=(TICKET_STAFF,), ticket_number=ticket_number
    )


def ticket_detail(request: HttpRequest, ticket_number: str) -> HttpResponse:
    """Unified ticket detail view for both team members and ops staff."""
    user = cast(User, request.user)

    is_ops = has_permission(user, TICKET_STAFF)  # staff see a ticket's history; teams see only its comments
    team = get_user_team(user)

    ticket = _visible_ticket(user, ticket_number)
    category = ticket.category

    comments = TicketComment.objects.filter(ticket=ticket).order_by("posted_at")
    attachments = TicketAttachment.objects.filter(ticket=ticket).order_by("uploaded_at")

    context: dict[str, object] = {
        "is_ops": is_ops,
        "team": team,
        "ticket": ticket,
        "category_name": category.display_name if category else "Unknown",
        "comments": comments,
        "comments_version": list_version(comments),
        "attachments": attachments,
        "status_display": ticket.status.upper().replace("_", " "),
        "actions": ticket_actions(user, ticket),
    }

    if is_ops:
        history = TicketHistory.objects.filter(ticket=ticket).order_by("-timestamp")[:20]
        context["is_box_reset"] = ticket.category_id == BOX_RESET_CATEGORY_ID
        context["variable_points"] = bool(category and category.variable_points)
        context["categories"] = TicketCategory.objects.all()
        context["history"] = history
        context["history_version"] = list_version(history)
        context["playbook_steps"] = category.steps if category else []
        context["staff_usernames"] = _ticketing_staff_usernames()

    return render(request, "ticket_detail.html", context)


@require_POST
def ticket_comment(request: HttpRequest, ticket_number: str) -> HttpResponse:
    """Post a comment to a ticket (team members on own tickets, ops on any)."""
    user = cast(User, request.user)
    authentik_username = user.username

    ticket = _visible_ticket(user, ticket_number)

    form = TicketCommentForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Comment cannot be empty")
        return redirect("ticket_detail", ticket_number=ticket.ticket_number)
    comment_text = form.cleaned_data["comment"]

    from ticketing.models import CommentRateLimit

    is_allowed, reason = CommentRateLimit.check_rate_limit(ticket.id, user.id)
    if not is_allowed:
        messages.error(request, reason)
        return redirect("ticket_detail", ticket_number=ticket.ticket_number)

    CommentRateLimit.objects.create(ticket=ticket, discord_id=user.id)

    comment = TicketComment.objects.create(
        ticket=ticket,
        author=user,
        comment_text=comment_text,
    )

    DiscordTask.enqueue(PostComment(ticket_id=ticket.id, comment_id=comment.id))

    logger.info(f"Comment posted on ticket {ticket.ticket_number} by {authentik_username} (web)")

    return redirect("ticket_detail", ticket_number=ticket.ticket_number)


def ticket_detail_dynamic(request: HttpRequest, ticket_number: str) -> HttpResponse:
    """Return dynamic ticket content (comments/history) for HTMX polling."""
    user = cast(User, request.user)

    ticket = _visible_ticket(user, ticket_number)

    comments = TicketComment.objects.filter(ticket=ticket).order_by("posted_at")
    # History holds staff notes, points and usernames; the team's own poll must not carry it
    history = (
        TicketHistory.objects.filter(ticket=ticket).order_by("-timestamp")[:20]
        if has_permission(user, TICKET_STAFF)
        else []
    )

    versions = {"comments_version": list_version(comments), "history_version": list_version(history)}
    asked = {name: request.GET[name] for name in versions if name in request.GET}
    if asked and all(versions[name] == value for name, value in asked.items()):
        return HttpResponse(status=204)

    return render(
        request,
        "ops_ticket_detail_dynamic.html",
        {"comments": comments, "history": history, **versions},
    )


@require_permission(TICKET_STAFF)
def ticket_service_checks(request: HttpRequest, ticket_number: str) -> HttpResponse:
    """Return recent Quotient service check results for a ticket."""
    from quotient.client import get_quotient_client, quotient_service_name

    ticket = get_object_or_404(Ticket.objects.select_related("team"), ticket_number=ticket_number)
    quotient_service = quotient_service_name(ticket.service_name, ticket.hostname)
    raw_checks = (
        get_quotient_client().get_service_checks(
            ticket.team.team_number,
            quotient_service,
            force_refresh=request.GET.get("refresh") == "1",
        )
        if quotient_service
        else []
    )

    checks = raw_checks[:10] if raw_checks is not None else None
    streak_count = (
        sum(1 for _ in takewhile(lambda c: c.passed == raw_checks[0].passed, raw_checks)) if raw_checks else 0
    )
    last_passed = next((c for c in raw_checks if c.passed), None) if raw_checks else None

    return render(
        request,
        "ticketing/service_checks.html",
        {
            "ticket": ticket,
            "quotient_service": quotient_service,
            "checks": checks,
            "streak_count": streak_count,
            "last_passed": last_passed,
        },
    )


@require_permission(TICKET_STAFF)
def ticket_box_reset_panel(request: HttpRequest, ticket_number: str) -> HttpResponse:
    """Return the matched Proxmox VM and snapshot selector for a Box Reset ticket."""
    from django.conf import settings

    from core.proxmox import get_proxmox_client, match_team_vm, normalize_ticket_host, team_pool_id

    user = cast(User, request.user)
    ticket = get_object_or_404(
        Ticket.objects.select_related("team", "assigned_to"),
        ticket_number=ticket_number,
        category_id=BOX_RESET_CATEGORY_ID,
    )
    team_number = ticket.team.team_number
    matched_vm = None
    snapshots = None
    proxmox_unreachable = False
    if normalize_ticket_host(team_number, ticket.hostname) is not None:
        client = get_proxmox_client()
        vms = client.list_team_vms(team_number)
        matched_vm = match_team_vm(vms, team_number, ticket.hostname) if vms is not None else None
        snapshots = client.list_snapshots(matched_vm.node, matched_vm.vmid) if matched_vm is not None else None
        proxmox_unreachable = vms is None or (matched_vm is not None and snapshots is None)

    default_snapshot = str(getattr(settings, "PROXMOX_DEFAULT_SNAPSHOT", "clean"))
    selected_snapshot = next(
        (s.name for s in (snapshots or ()) if s.name == default_snapshot),
        snapshots[0].name if snapshots else "",
    )

    return render(
        request,
        "ticketing/box_reset_panel.html",
        {
            "ticket": ticket,
            "actions": ticket_actions(user, ticket),
            "proxmox_unreachable": proxmox_unreachable,
            "matched_vm": matched_vm,
            "snapshots": snapshots,
            "selected_snapshot": selected_snapshot,
            "pool_name": team_pool_id(team_number),
        },
    )
