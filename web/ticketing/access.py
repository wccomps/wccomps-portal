"""Who works tickets: one name for the ticket staff, and the rule for acting on a claimed ticket."""

from django.contrib.auth.models import AnonymousUser, User

from core.auth_utils import has_permission
from ticketing.models import Ticket

# Ticketing staff see and work every team's tickets; the permission's groups hold ticketing admins and portal admins
TICKET_STAFF = "ticketing_support"

# What a staff member who neither holds the ticket nor is a ticketing admin is told (a ticket's state, not a role)
NOT_YOUR_TICKET = "Only the support member it is assigned to, or a ticketing admin, can do that."


def is_ticket_staff(user: User | AnonymousUser) -> bool:
    return has_permission(user, TICKET_STAFF)


def works_ticket(user: User, ticket: Ticket) -> bool:
    """Whether the person may resolve, unclaim or recategorize the ticket: its assignee, or a ticketing admin."""
    return ticket.assigned_to_id == user.id or has_permission(user, "ticketing_admin")
