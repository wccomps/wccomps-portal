"""What the bot takes for a new ticket: the web form's rules (ticketing.forms.CreateTicketForm), so a ticket filed in
Discord holds only what one filed on the web may."""

from dataclasses import dataclass

from core.tickets_config import TicketCategoryConfig, get_all_categories
from ticketing.forms import CreateTicketForm
from ticketing.models import TicketCategory


@dataclass(frozen=True)
class TicketInput:
    category: TicketCategory
    config: TicketCategoryConfig
    description: str
    hostname: str
    ip_address: str
    service_name: str


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
    form = CreateTicketForm(
        {
            "title": "ticket",  # the category's name is the title
            "category": category,
            "description": description,
            "hostname": hostname,
            "ip_address": ip_address,
            "service_name": service_name,
        }
    )
    if not form.is_valid():
        return " ".join(
            f"{name.replace('_', ' ')}: {error}" for name, errors in form.errors.items() for error in errors
        )
    categories = get_all_categories(user_creatable_only=not staff)
    category_id = form.cleaned_data["category"]
    if category_id not in categories:
        return "Invalid ticket category."
    return TicketInput(
        category=TicketCategory.objects.get(pk=category_id),
        config=categories[category_id],
        description=form.cleaned_data["description"],
        hostname=form.cleaned_data["hostname"],
        ip_address=form.cleaned_data["ip_address"],
        service_name=form.cleaned_data["service_name"],
    )
