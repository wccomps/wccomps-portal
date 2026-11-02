import logging
from typing import cast

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from scoring.quotient_sync import ticket_infrastructure

from core.auth_utils import get_user_team, has_permission
from team.models import Team
from ticketing.forms import CreateTicketPageForm

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

    # The last synced Quotient metadata: the pickers keep it while Quotient is down
    box_names, box_ip_map, service_choices = ticket_infrastructure()

    form = CreateTicketPageForm(
        request.POST if request.method == "POST" else None,
        staff=is_admin,
        box_ip_map=box_ip_map,
        service_choices=service_choices,
        teams=teams,
        own_team=team,
        box_names=box_names,
    )

    def form_page(status: int = 200) -> HttpResponse:
        return render(request, "create_ticket.html", {"team": team, "form": form}, status=status)

    if request.method == "POST":
        valid = form.is_valid()
        team = form.cleaned_data["team"]
        if not valid:
            return form_page()

        from ticketing.utils import TicketRateLimitError, create_ticket_atomic

        cd = form.cleaned_data
        try:
            ticket = create_ticket_atomic(
                team=team,
                category=form.cleaned_data["category"],
                title=cd["title"],
                description=cd["description"],
                hostname=cd["hostname"],
                ip_address=cd["ip_address"],
                service_name=cd["service_name"],
                actor_username=authentik_username,
                enforce_team_limit=not is_admin,
            )

            logger.info(f"Ticket {ticket.ticket_number} created via web by {authentik_username} for {team.team_name}")

            return redirect("ticket_detail", ticket_number=ticket.ticket_number)

        except TicketRateLimitError as e:
            logger.warning(f"Ticket rate limit hit by {authentik_username} for {team.team_name}")
            form.add_error(None, str(e))
            return form_page(status=429)
        except Exception:
            logger.exception("Failed to create ticket")
            form.add_error(
                None, "Failed to create ticket. Please try again or contact support if the problem persists."
            )
            return form_page()

    return form_page()
