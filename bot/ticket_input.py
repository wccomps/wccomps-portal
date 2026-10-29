"""What the bot takes for a new ticket: the web form's rules (ticketing.forms.CreateTicketForm), so a ticket filed in
Discord holds only what one filed on the web may."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from ticketing.forms import CreateTicketForm, TicketResolveForm
from ticketing.models import TicketCategory


@dataclass(frozen=True)
class TicketInput:
    category: TicketCategory
    description: str
    hostname: str
    ip_address: str
    service_name: str


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


def read_ticket(
    category: str,
    *,
    staff: bool,
    description: str = "",
    hostname: str = "",
    ip_address: str = "",
    service_name: str = "",
) -> TicketInput | str:
    """The ticket these fields describe, cleaned as the web form cleans them, or what is wrong with them. A team
    files only in the categories teams may create; staff in any (as on the web)."""
    box_ip_map: dict[str, str] | None = None
    service_choices: list[dict[str, str]] = []
    if (hostname.strip() and not ip_address.strip()) or (service_name.strip() and not hostname.strip()):
        from bot.cogs.ticketing import TicketingCog

        _, box_ip_map, service_choices = TicketingCog._load_infrastructure_data()
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
    return TicketInput(
        category=form.cleaned_data["category"],
        description=form.cleaned_data["description"],
        hostname=form.cleaned_data["hostname"],
        ip_address=form.cleaned_data["ip_address"],
        service_name=form.cleaned_data["service_name"],
    )
