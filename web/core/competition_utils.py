from collections.abc import Callable

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


def _delete(rows: QuerySet[Model]) -> int:
    return rows.delete()[0]


def _reset_ticket_counters(teams: QuerySet[Model]) -> int:
    return teams.update(ticket_counter=0)


# What the wipe clears, in order: (count label, the rows, how they are cleared)
COMPETITION_DATA: tuple[tuple[str, Callable[[], QuerySet[Model]], Callable[[QuerySet[Model]], int]], ...] = (
    ("TicketAttachment", TicketAttachment.objects.all, _delete),
    ("TicketComment", TicketComment.objects.all, _delete),
    ("TicketHistory", TicketHistory.objects.all, _delete),
    ("Ticket", Ticket.objects.all, _delete),
    ("TeamTicketCounter", lambda: Team.objects.filter(ticket_counter__gt=0), _reset_ticket_counters),
    ("RedTeamScreenshot", RedTeamScreenshot.objects.all, _delete),
    ("IncidentScreenshot", IncidentScreenshot.objects.all, _delete),
    ("IncidentReport", IncidentReport.objects.all, _delete),
    ("RedTeamScore", RedTeamScore.objects.all, _delete),
    ("InjectScore", InjectScore.objects.all, _delete),
    ("OrangeTeamScore", OrangeTeamScore.objects.all, _delete),
    ("ServiceDetail", ServiceDetail.objects.all, _delete),
    ("ServiceScore", ServiceScore.objects.all, _delete),
    ("ScoringExclusion", ScoringExclusion.objects.all, _delete),
    ("OrangeAssignment", OrangeAssignment.objects.all, _delete),
    ("OrangeCheckIn", OrangeCheckIn.objects.all, _delete),
    # Blue team Discord links only (staff/volunteer links preserved)
    ("BlueTeamDiscordLink", lambda: DiscordLink.objects.filter(team__isnull=False), _delete),
)


def wipe_competition_data() -> dict[str, int]:
    """Wipe competition data for a fresh start, returning deleted counts by model plus TeamTicketCounter, the
    number of team ticket counters reset.

    Preserves teams, orange checks (reusable rubrics), AuditLog, BotState, DiscordTask and LinkToken/LinkAttempt.
    Each team's ticket counter is reset with its tickets, so the next event's numbering starts at T0NN-001.
    """
    return {label: clear(rows()) for label, rows, clear in COMPETITION_DATA}


def live_competition_data_exists() -> bool:
    """Whether any of the data the competition wipe deletes is still here (tickets, scores, incidents, orange team
    assignments and check-ins, team Discord links)."""
    return any(rows().exists() for _, rows, _ in COMPETITION_DATA)
