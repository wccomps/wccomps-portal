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


def wipe_competition_data() -> dict[str, int]:
    """Wipe competition data for a fresh start, returning deleted counts by model plus TeamTicketCounter, the
    number of team ticket counters reset.

    Preserves teams, orange checks (reusable rubrics), AuditLog, BotState, DiscordTask and LinkToken/LinkAttempt.
    Each team's ticket counter is reset with its tickets, so the next event's numbering starts at T0NN-001.
    """
    counts = {
        "TicketAttachment": TicketAttachment.objects.all().delete()[0],
        "TicketComment": TicketComment.objects.all().delete()[0],
        "TicketHistory": TicketHistory.objects.all().delete()[0],
        "Ticket": Ticket.objects.all().delete()[0],
        "TeamTicketCounter": Team.objects.filter(ticket_counter__gt=0).update(ticket_counter=0),
        "RedTeamScreenshot": RedTeamScreenshot.objects.all().delete()[0],
        "IncidentScreenshot": IncidentScreenshot.objects.all().delete()[0],
        "IncidentReport": IncidentReport.objects.all().delete()[0],
        "RedTeamScore": RedTeamScore.objects.all().delete()[0],
        "InjectScore": InjectScore.objects.all().delete()[0],
        "OrangeTeamScore": OrangeTeamScore.objects.all().delete()[0],
        "ServiceDetail": ServiceDetail.objects.all().delete()[0],
        "ServiceScore": ServiceScore.objects.all().delete()[0],
        "ScoringExclusion": ScoringExclusion.objects.all().delete()[0],
        "OrangeAssignment": OrangeAssignment.objects.all().delete()[0],
        "OrangeCheckIn": OrangeCheckIn.objects.all().delete()[0],
        # Blue team Discord links only (staff/volunteer links preserved)
        "BlueTeamDiscordLink": DiscordLink.objects.filter(team__isnull=False).delete()[0],
    }
    return counts


def live_competition_data_exists() -> bool:
    """Whether results from a past event are still in the portal (not yet finalized and cleared)."""
    models = (Ticket, ServiceScore, ServiceDetail, InjectScore, OrangeTeamScore, RedTeamScore, IncidentReport)
    return any(model.objects.exists() for model in models)
