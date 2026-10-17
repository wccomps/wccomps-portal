"""Who works tickets: the ticket staff's permission, and what a page offers a person on a ticket."""

from dataclasses import dataclass

from django.contrib.auth.models import User

from core.auth_utils import has_permission
from ticketing.lifecycle import assignee_refusal, refusal
from ticketing.models import Ticket

# Ticketing staff see and work every team's tickets; the permission's groups hold ticketing admins and portal admins
TICKET_STAFF = "ticketing_support"


@dataclass(frozen=True)
class TicketActions:
    """The ticket actions a page offers: the ones the lifecycle would let this person take now."""

    resolve: bool
    unclaim: bool
    recategorize: bool
    reassign: bool


def ticket_actions(user: User, ticket: Ticket) -> TicketActions:
    """What to offer on a claimed ticket (resolve, unclaim, change its category) and on any ticket (give it to
    someone): each where its status allows it and assignee_refusal lets the person through."""
    if not has_permission(user, TICKET_STAFF):
        return TicketActions(resolve=False, unclaim=False, recategorize=False, reassign=False)
    claimed = ticket.status == Ticket.STATUS_CLAIMED
    held = claimed and assignee_refusal(ticket, user, "resolve") is None
    return TicketActions(
        resolve=held and refusal(ticket, "resolve") is None,
        unclaim=held and refusal(ticket, "unclaim") is None,
        recategorize=held,
        reassign=assignee_refusal(ticket, user, "reassign") is None,
    )
