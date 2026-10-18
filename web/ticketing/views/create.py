import contextlib
import logging
from typing import cast

from django.contrib.auth.models import User
from django.core.exceptions import BadRequest, PermissionDenied
from django.db import transaction
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render

from core.auth_utils import get_user_team, has_permission
from core.discord_tasks import TicketCreatedWeb
from core.models import DiscordTask
from core.tickets_config import get_all_categories
from team.models import Team
from ticketing.forms import CreateTicketForm

logger = logging.getLogger(__name__)


def create_ticket(request: HttpRequest) -> HttpResponse:
    """Create a new support ticket (web form alternative to Discord command)."""
    user = cast(User, request.user)
    authentik_username = user.username
    team = get_user_team(user)

    # Allow admins (incl. ticketing admins, filing on a team's behalf) to create tickets for any team
    is_admin = (
        has_permission(user, "gold_team") or has_permission(user, "admin") or has_permission(user, "ticketing_admin")
    )
    teams = Team.objects.filter(is_active=True).order_by("team_number") if is_admin else None

    if not is_admin and not team:
        raise PermissionDenied("You must be a team member to create tickets.")

    from quotient.client import QuotientAPIError, get_quotient_client

    infrastructure = None
    service_choices: list[dict[str, str]] = []
    box_names: list[str] = []
    box_ip_map: dict[str, str] = {}

    try:
        quotient_client = get_quotient_client()
        infrastructure = quotient_client.get_infrastructure()
        service_choices = quotient_client.get_service_choices()
        box_names = quotient_client.get_box_names()
        if infrastructure:
            for box in infrastructure.boxes:
                box_ip_map[box.name] = box.ip
    except QuotientAPIError:
        logger.warning("Quotient API unavailable, ticket form will have limited functionality")

    # Staff file tickets in any category; teams only in user-creatable ones.
    categories = get_all_categories(user_creatable_only=not is_admin)

    def form_page(error: str | None = None, status: int = 200) -> HttpResponse:
        context: dict[str, object] = {
            "team": team,
            "teams": teams,
            "categories": categories,
            "service_choices": service_choices,
            "box_names": box_names,
            "box_ip_map": box_ip_map,
        }
        if error:
            context["error"] = error
            context["form_data"] = request.POST
        return render(request, "create_ticket.html", context, status=status)

    if request.method == "POST":
        form = CreateTicketForm(
            request.POST,
            staff=is_admin,
            categories=categories,
            box_ip_map=box_ip_map,
            service_choices=service_choices,
        )
        valid = form.is_valid()
        if is_admin and form.cleaned_data.get("team_id"):
            with contextlib.suppress(Team.DoesNotExist):
                team = Team.objects.get(id=form.cleaned_data["team_id"], is_active=True)
        if not valid:
            return form_page(" ".join(str(err) for errors in form.errors.values() for err in errors))

        if is_admin and not team:
            return form_page("Please select a team.")

        if not team or not form.category_obj:
            raise BadRequest("Team is required to create a ticket.")

        from ticketing.utils import TicketRateLimitError, create_ticket_atomic

        cd = form.cleaned_data
        try:
            # One transaction, so a ticket never exists without the task that gives it a thread.
            with transaction.atomic():
                ticket = create_ticket_atomic(
                    team=team,
                    category=form.category_obj,
                    title=cd["title"],
                    description=cd["description"],
                    hostname=cd["hostname"],
                    ip_address=cd["ip_address"],
                    service_name=cd["service_name"],
                    actor_username=authentik_username,
                    enforce_team_limit=not is_admin,
                )
                DiscordTask.enqueue(TicketCreatedWeb(ticket_id=ticket.id))

            logger.info(f"Ticket {ticket.ticket_number} created via web by {authentik_username} for {team.team_name}")

            return redirect("ticket_detail", ticket_number=ticket.ticket_number)

        except TicketRateLimitError as e:
            logger.warning(f"Ticket rate limit hit by {authentik_username} for {team.team_name}")
            return form_page(str(e), status=429)
        except Exception:
            logger.exception("Failed to create ticket")

            return form_page("Failed to create ticket. Please try again or contact support if the problem persists.")

    return form_page()
