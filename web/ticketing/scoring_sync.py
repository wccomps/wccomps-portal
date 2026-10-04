"""Keep each team's ServiceScore.point_adjustments in step with its approved ticket charges.

A ticket's `points_charged` is the (positive) charge applied when the ticket is approved;
the scoring report deducts it via ServiceScore.point_adjustments (stored negative). These
helpers recompute that deduction from the approved tickets, so approving / reopening /
cancelling a ticket updates the scorecard with no manual step.
"""

from decimal import Decimal

from django.db import models
from scoring.models import ServiceScore

from team.models import Team
from ticketing.models import Ticket


def recompute_team_ticket_adjustment(team: Team) -> None:
    """Set the team's ServiceScore.point_adjustments to minus the sum of its approved charges."""
    total = Ticket.objects.filter(team=team, is_approved=True).aggregate(s=models.Sum("points_charged"))["s"] or 0
    ServiceScore.objects.update_or_create(team=team, defaults={"point_adjustments": Decimal(-total)})


def recompute_all_ticket_adjustments() -> int:
    """Recompute every team that has at least one ticket. Returns the number of teams touched."""
    team_ids = list(Ticket.objects.values_list("team_id", flat=True).distinct())
    teams = Team.objects.filter(pk__in=team_ids)
    for team in teams:
        recompute_team_ticket_adjustment(team)
    return len(teams)
