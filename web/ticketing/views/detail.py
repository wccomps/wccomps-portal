import hashlib
import logging
from collections.abc import Iterable
from typing import cast

from django.contrib import messages
from django.contrib.auth.models import User
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from core.auth_utils import get_user_team, has_permission, team_record_or_404
from core.discord_tasks import PostComment
from core.models import DiscordTask
from core.tickets_config import get_all_categories, get_category_config
from ticketing.access import TICKET_STAFF, ticket_actions
from ticketing.forms import TicketCommentForm
from ticketing.models import Ticket, TicketAttachment, TicketComment, TicketHistory

logger = logging.getLogger(__name__)


def list_version(rows: Iterable[TicketComment | TicketHistory]) -> str:
    """A short marker of the rows a polled list shows. The page and each poll carry it, and a poll whose marker
    still matches answers 204, so htmx swaps nothing and the aria-live region stays quiet."""
    return hashlib.sha256(",".join(str(row.pk) for row in rows).encode()).hexdigest()[:16]


def _ticketing_staff_usernames() -> list[str]:
    """Usernames that can work tickets, for the assign box's suggestions."""
    from functools import reduce
    from operator import or_

    from django.db.models import Q

    from core.models import UserGroups
    from core.permission_constants import PERMISSION_MAP

    in_any_group = reduce(or_, (Q(groups__contains=[g]) for g in PERMISSION_MAP[TICKET_STAFF]))
    return list(
        UserGroups.objects.filter(in_any_group).order_by("user__username").values_list("user__username", flat=True)
    )


def _visible_ticket(user: User, ticket_number: str) -> Ticket:
    """The ticket as the person may see it: ticketing staff see every ticket, a team its own (another team's is a
    404, as a missing one is)."""
    return team_record_or_404(
        Ticket.objects.select_related("team"), user, staff=(TICKET_STAFF,), ticket_number=ticket_number
    )


def ticket_detail(request: HttpRequest, ticket_number: str) -> HttpResponse:
    """Unified ticket detail view for both team members and ops staff."""
    user = cast(User, request.user)
    authentik_username = user.username

    is_ops = has_permission(user, TICKET_STAFF)  # staff see a ticket's history; teams see only its comments
    team = get_user_team(user)

    ticket = _visible_ticket(user, ticket_number)

    cat_info = get_category_config(ticket.category_id) or {}
    comments = TicketComment.objects.filter(ticket=ticket).order_by("posted_at")
    attachments = TicketAttachment.objects.filter(ticket=ticket).order_by("uploaded_at")

    context: dict[str, object] = {
        "is_ops": is_ops,
        "authentik_username": authentik_username,
        "team": team,
        "ticket": ticket,
        "category_name": cat_info.get("display_name", "Unknown"),
        "comments": comments,
        "comments_version": list_version(comments),
        "attachments": attachments,
        "status_display": ticket.status.upper().replace("_", " "),
        "actions": ticket_actions(user, ticket),
    }

    if is_ops:
        history = TicketHistory.objects.filter(ticket=ticket).order_by("-timestamp")[:20]
        context["variable_points"] = cat_info.get("variable_points", False)
        context["categories"] = get_all_categories()
        context["history"] = history
        context["history_version"] = list_version(history)
        context["playbook_steps"] = cat_info.get("playbook_steps", [])
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
        {"ticket": ticket, "comments": comments, "history": history, **versions},
    )
