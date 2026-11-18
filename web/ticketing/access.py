"""Who works tickets: the ticket staff's permission, and what a page offers a person on a ticket."""

from dataclasses import dataclass

from django.contrib.auth.models import User

from core.auth_utils import get_user_team, has_permission
from team.models import Team
from ticketing.lifecycle import assignee_refusal, refusal
from ticketing.models import Ticket

# Ticketing staff see and work every team's tickets; the permission's groups hold ticketing admins and portal admins
TICKET_STAFF = "ticketing_support"


def ticket_open_to(ticket: Ticket, *, staff: bool, team: Team | None) -> bool:
    """Whether someone may see and write to the ticket: ticket staff, or a member of its team (team: the one the
    person's own groups put them on)."""
    return staff or (team is not None and team.pk == ticket.team_id)


def ticket_open_to_user(ticket: Ticket, user: User) -> bool:
    """ticket_open_to for a portal account, from its own groups as on the web: none while it is disabled."""
    return ticket_open_to(ticket, staff=has_permission(user, TICKET_STAFF), team=get_user_team(user))


@dataclass(frozen=True)
class TicketActions:
    """The ticket actions a page offers: the ones the lifecycle would let this person take now."""

    resolve: bool
    unclaim: bool
    recategorize: bool
    reassign: bool
    reopen: bool
    adjust_points: bool


def ticket_actions(user: User, ticket: Ticket) -> TicketActions:
    if not has_permission(user, TICKET_STAFF):
        return TicketActions(
            resolve=False,
            unclaim=False,
            recategorize=False,
            reassign=False,
            reopen=False,
            adjust_points=False,
        )
    claimed = ticket.status == Ticket.STATUS_CLAIMED
    held = claimed and assignee_refusal(ticket, user, "resolve") is None
    return TicketActions(
        resolve=held and refusal(ticket, "resolve") is None,
        unclaim=held and refusal(ticket, "unclaim") is None,
        recategorize=held,
        reassign=assignee_refusal(ticket, user, "reassign") is None,
        reopen=refusal(ticket, "reopen") is None and assignee_refusal(ticket, user, "reopen") is None,
        adjust_points=(
            refusal(ticket, "adjust_points") is None and assignee_refusal(ticket, user, "adjust_points") is None
        ),
    )
