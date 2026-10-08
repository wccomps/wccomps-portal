import logging
from typing import cast

from django.contrib import messages
from django.contrib.auth.models import User
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from core.auth_utils import get_user_team, has_permission
from core.discord_tasks import PostComment
from core.models import DiscordTask
from core.tickets_config import get_all_categories, get_category_config
from ticketing.forms import TicketCommentForm
from ticketing.models import Ticket, TicketAttachment, TicketComment, TicketHistory

logger = logging.getLogger(__name__)


def _ticketing_staff_usernames() -> list[str]:
    """Usernames that can work tickets, for the assign box's suggestions."""
    from functools import reduce
    from operator import or_

    from django.db.models import Q

    from core.models import UserGroups
    from core.permission_constants import PERMISSION_MAP

    in_any_group = reduce(or_, (Q(groups__contains=[g]) for g in PERMISSION_MAP["ticketing_support"]))
    return list(
        UserGroups.objects.filter(in_any_group).order_by("user__username").values_list("user__username", flat=True)
    )


def _can_access(user: User, ticket: Ticket) -> bool:
    """Ticketing staff see every ticket; a team sees its own."""
    if has_permission(user, "ticketing_support"):
        return True
    team = get_user_team(user)
    return team is not None and ticket.team_id == team.id


def _is_ops(user: User) -> bool:
    """Ticketing staff, who see a ticket's history; teams see only its comments."""
    return (
        has_permission(user, "ticketing_support")
        or has_permission(user, "ticketing_admin")
        or has_permission(user, "admin")
    )


def ticket_detail(request: HttpRequest, ticket_number: str) -> HttpResponse:
    """Unified ticket detail view for both team members and ops staff."""
    user = cast(User, request.user)
    authentik_username = user.username

    is_ops = _is_ops(user)
    team = get_user_team(user)
    is_ticketing_admin = has_permission(user, "ticketing_admin")
    is_ticketing_support = has_permission(user, "ticketing_support")

    try:
        ticket = Ticket.objects.select_related("team").get(ticket_number=ticket_number)
    except Ticket.DoesNotExist:
        return render(
            request,
            "error.html",
            {
                "error": "Ticket not found",
                "message": f"Ticket {ticket_number} does not exist.",
            },
        )

    if not _can_access(user, ticket):
        return render(
            request,
            "error.html",
            {"error": "Access Denied", "message": "You do not have permission to view this ticket."},
            status=403,
        )

    cat_info = get_category_config(ticket.category_id) or {}
    comments = TicketComment.objects.filter(ticket=ticket).order_by("posted_at")
    attachments = TicketAttachment.objects.filter(ticket=ticket).order_by("uploaded_at")

    context: dict[str, object] = {
        "is_ops": is_ops,
        "is_ticketing_admin": is_ticketing_admin,
        "is_ticketing_support": is_ticketing_support,
        "authentik_username": authentik_username,
        "team": team,
        "ticket": ticket,
        "category_name": cat_info.get("display_name", "Unknown"),
        "comments": comments,
        "attachments": attachments,
        "status_display": ticket.status.upper().replace("_", " "),
    }

    if is_ops:
        history = TicketHistory.objects.filter(ticket=ticket).order_by("-timestamp")[:20]
        context["variable_points"] = cat_info.get("variable_points", False)
        context["categories"] = get_all_categories()
        context["history"] = history
        context["playbook_steps"] = cat_info.get("playbook_steps", [])
        if is_ticketing_support or is_ticketing_admin:
            context["staff_usernames"] = _ticketing_staff_usernames()

        # Preserve filter state from referrer for back navigation
        context["status_filter"] = request.GET.get("status", "")
        context["category_filter"] = request.GET.get("category", "")
        context["team_filter"] = request.GET.get("team", "")
        context["assignee_filter"] = request.GET.get("assignee", "")
        context["search_filter"] = request.GET.get("search", "")
        context["sort_filter"] = request.GET.get("sort", "")
        context["page_filter"] = request.GET.get("page", "")
        context["page_size"] = request.GET.get("page_size", "")

    return render(request, "ticket_detail.html", context)


@require_POST
def ticket_comment(request: HttpRequest, ticket_number: str) -> HttpResponse:
    """Post a comment to a ticket (team members on own tickets, ops on any)."""
    user = cast(User, request.user)
    authentik_username = user.username

    try:
        ticket = Ticket.objects.select_related("team").get(ticket_number=ticket_number)
    except Ticket.DoesNotExist:
        return render(
            request,
            "error.html",
            {"error": "Not Found", "message": "The requested ticket was not found."},
            status=404,
        )

    if not _can_access(user, ticket):
        return render(
            request,
            "error.html",
            {"error": "Access Denied", "message": "You do not have permission to perform this action."},
            status=403,
        )

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

    try:
        ticket = Ticket.objects.select_related("team").get(ticket_number=ticket_number)
    except Ticket.DoesNotExist:
        return HttpResponse("Ticket not found", status=404)

    if not _can_access(user, ticket):
        return HttpResponse("Access denied", status=403)

    comments = TicketComment.objects.filter(ticket=ticket).order_by("posted_at")
    # History holds staff notes, points and usernames; the team's own poll must not carry it
    history = TicketHistory.objects.filter(ticket=ticket).order_by("-timestamp")[:20] if _is_ops(user) else []

    return render(
        request,
        "ops_ticket_detail_dynamic.html",
        {
            "ticket": ticket,
            "comments": comments,
            "history": history,
        },
    )
