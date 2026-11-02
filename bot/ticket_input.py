"""What the bot takes for a new ticket: the web form's rules (ticketing.forms.CreateTicketForm), so a ticket filed in
Discord holds only what one filed on the web may."""

from collections.abc import Iterable, Mapping

from scoring.quotient_sync import ticket_infrastructure

from team.models import Team
from ticketing.forms import CreateTicketForm, TicketResolveForm
from ticketing.models import Ticket, TicketCategory


def form_errors(errors: Mapping[str, Iterable[object]]) -> str:
    """A form's or model's errors as one reply, each after its field's name."""
    return " ".join(f"{name.replace('_', ' ')}: {error}" for name, messages in errors.items() for error in messages)


def read_resolution(notes: str, points: str) -> tuple[str, int | None] | str:
    """Resolution notes and points as the web's resolve form reads them (points within the column they are charged
    to), or what is wrong with them."""
    form = TicketResolveForm({"resolution_notes": notes, "points_override": points})
    if not form.is_valid():
        return form_errors(form.errors)
    return form.cleaned_data["resolution_notes"], form.cleaned_data["points_override"]


def file_ticket(
    team: Team,
    category: str,
    *,
    staff: bool,
    actor: str,
    description: str = "",
    hostname: str = "",
    ip_address: str = "",
    service_name: str = "",
) -> tuple[Ticket, TicketCategory] | str:
    """File a ticket with the fields cleaned as the web form cleans them: the ticket (its thread task queued) and its
    category, or why not: a field the form refuses, or the team's ticket rate limit. A team files only in the
    categories teams may create and within the rate limit; staff in any, past it (as on the web)."""
    from ticketing.utils import TicketRateLimitError, create_ticket_atomic

    box_ip_map: dict[str, str] | None = None
    service_choices: list[dict[str, str]] = []
    if (hostname.strip() and not ip_address.strip()) or (service_name.strip() and not hostname.strip()):
        _, box_ip_map, service_choices = ticket_infrastructure()
    form = CreateTicketForm(
        {
            "title": "ticket",
            "category": category,
            "description": description,
            "hostname": hostname,
            "ip_address": ip_address,
            "service_name": service_name,
        },
        staff=staff,
        box_ip_map=box_ip_map,
        service_choices=service_choices,
    )
    if not form.is_valid():
        return form_errors(form.errors)
    filed_in = form.cleaned_data["category"]
    try:
        ticket = create_ticket_atomic(
            team=team,
            category=filed_in,
            title=filed_in.display_name,
            description=form.cleaned_data["description"],
            hostname=form.cleaned_data["hostname"],
            ip_address=form.cleaned_data["ip_address"],
            service_name=form.cleaned_data["service_name"],
            actor_username=actor,
            enforce_team_limit=not staff,
        )
    except TicketRateLimitError as e:
        return str(e)
    return ticket, filed_in
