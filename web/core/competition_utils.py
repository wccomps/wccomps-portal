from collections.abc import Callable
from dataclasses import dataclass

from django.db.models import Model, QuerySet
from orange_team.models import OrangeAssignment, OrangeCheckIn
from scoring.models import (
    IncidentReport,
    IncidentScreenshot,
    InjectScore,
    OrangeTeamScore,
    RedTeamScore,
    RedTeamScreenshot,
    ScoringExclusion,
    ServiceDetail,
    ServiceScore,
)

from team.models import DiscordLink, Team
from ticketing.models import Ticket, TicketAttachment, TicketComment, TicketHistory

# What the wipe deletes, in order: (count label, the rows). While any of it remains, or a team's ticket counter has
# been used, a school list import is blocked (registration.services.import_problem).
COMPETITION_DATA: tuple[tuple[str, Callable[[], QuerySet[Model]]], ...] = (
    ("TicketAttachment", TicketAttachment.objects.all),
    ("TicketComment", TicketComment.objects.all),
    ("TicketHistory", TicketHistory.objects.all),
    ("Ticket", Ticket.objects.all),
    ("RedTeamScreenshot", RedTeamScreenshot.objects.all),
    ("IncidentScreenshot", IncidentScreenshot.objects.all),
    ("IncidentReport", IncidentReport.objects.all),
    ("RedTeamScore", RedTeamScore.objects.all),
    ("InjectScore", InjectScore.objects.all),
    ("OrangeTeamScore", OrangeTeamScore.objects.all),
    ("ServiceDetail", ServiceDetail.objects.all),
    ("ServiceScore", ServiceScore.objects.all),
    ("ScoringExclusion", ScoringExclusion.objects.all),
    ("OrangeAssignment", OrangeAssignment.objects.all),
    ("OrangeCheckIn", OrangeCheckIn.objects.all),
    # Blue team Discord links only (staff/volunteer links preserved)
    ("BlueTeamDiscordLink", lambda: DiscordLink.objects.filter(team__isnull=False)),
)


def _used_ticket_counters() -> QuerySet[Team]:
    return Team.objects.filter(ticket_counter__gt=0)


@dataclass(frozen=True)
class WipedData:
    """What wipe_competition_data did: deleted rows by label, and how many team ticket counters it reset."""

    deleted: dict[str, int]
    ticket_counters_reset: int


def wipe_competition_data() -> WipedData:
    """Wipe competition data for a fresh start.

    Preserves teams, orange checks (reusable rubrics), AuditLog, BotState, DiscordTask and LinkToken/LinkAttempt.
    Each team's ticket counter is reset with its tickets, so the next event's numbering starts at T0NN-001.
    """
    deleted = {label: rows().delete()[0] for label, rows in COMPETITION_DATA}
    return WipedData(deleted=deleted, ticket_counters_reset=_used_ticket_counters().update(ticket_counter=0))


def live_competition_data_exists() -> bool:
    """Whether any row the competition wipe deletes still exists, or a ticket counter it resets is in use."""
    return _used_ticket_counters().exists() or any(rows().exists() for _, rows in COMPETITION_DATA)
