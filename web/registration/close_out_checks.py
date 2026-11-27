"""Close Out notices about unfinished work, retained evidence and the recorded mail fingerprint."""

from collections.abc import Callable
from dataclasses import dataclass

from orange_team.models import OrangeAssignment
from scoring.calculator import Standing
from scoring.models import IncidentReport, InjectScore, OrangeTeamScore, RedTeamScore
from scoring.snapshot import scorecard_fingerprint

from ticketing.models import Ticket

from .models import Event
from .services import CloseOutCards, current_snapshots
from .stage import ScorecardsEmailed


@dataclass(frozen=True)
class CloseOutWarning:
    """A nonblocking notice and the related page."""

    text: str
    url_name: str | None = None


@dataclass(frozen=True)
class UnfinishedWork:
    count: Callable[[], int]
    one: str
    many: str
    url_name: str


UNFINISHED_WORK = (
    UnfinishedWork(
        lambda: InjectScore.objects.filter(is_approved=False).count(),
        "inject grade is not approved",
        "inject grades are not approved",
        "scoring:inject_grades_review",
    ),
    UnfinishedWork(
        lambda: Ticket.objects.filter(status__in=[Ticket.STATUS_OPEN, Ticket.STATUS_CLAIMED]).count(),
        "ticket is not resolved",
        "tickets are not resolved",
        "ticket_list",
    ),
    UnfinishedWork(
        lambda: Ticket.objects.filter(status=Ticket.STATUS_RESOLVED, is_approved=False).count(),
        "resolved ticket is not approved",
        "resolved tickets are not approved",
        "ops_review_tickets",
    ),
    UnfinishedWork(
        lambda: IncidentReport.objects.filter(is_approved=False).count(),
        "incident report is not reviewed",
        "incident reports are not reviewed",
        "scoring:review_incidents",
    ),
    UnfinishedWork(
        lambda: RedTeamScore.objects.filter(is_approved=False).count(),
        "red team finding is not approved",
        "red team findings are not approved",
        "scoring:red_team_findings",
    ),
    UnfinishedWork(
        lambda: OrangeTeamScore.objects.filter(is_approved=False).count(),
        "orange check score is not approved",
        "orange check scores are not approved",
        "scoring:review_orange",
    ),
    UnfinishedWork(
        lambda: OrangeAssignment.objects.unfinished().count(),
        "orange assignment is not scored and reviewed",
        "orange assignments are not scored and reviewed",
        "orange_team:check_list",
    ),
)


def _sentence(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}."


def close_out_warnings(event: Event | None, cards: CloseOutCards | None) -> list[CloseOutWarning]:
    """Unfinished work, preservation notices and mail comparison against the selected cards."""
    warnings = [
        CloseOutWarning(_sentence(count, work.one, work.many), work.url_name)
        for work in UNFINISHED_WORK
        if (count := work.count())
    ]
    if cards is not None:
        omitted = ", ".join(
            f"Team {a.team_number} ({a.school_name}, {'active' if a.is_active else 'inactive'})"
            for a in cards.roster
            if a.school_id not in cards.schools
        )
        if omitted:
            warnings.append(
                CloseOutWarning(
                    f"No card in the set being archived for: {omitted}. "
                    "The close-time roster still includes these assignments; existing packet distributions "
                    "are archived by slot."
                )
            )
    return warnings + [CloseOutWarning(text) for text in _scorecard_warnings(event, cards)]


def scorecards_fingerprint(event: Event | None, standings: list[Standing] | None = None) -> str:
    """Fingerprint of assigned schools' current cards in the supplied or freshly computed standings."""
    return scorecard_fingerprint(current_snapshots(event, standings))


def _scorecard_warnings(event: Event | None, cards: CloseOutCards | None) -> list[str]:
    """Compare the selected cards to the last recorded fingerprint, not to individual deliveries."""
    if event is None:
        return ["No event is open, so scorecard email records cannot be matched to an event."]
    emailed = ScorecardsEmailed.latest(event)
    if emailed is None:
        return ["No scorecard email is recorded for this event."]
    warnings = []
    if failed := emailed.failed:
        one = "scorecard email failed in the last recorded send"
        many = "scorecard emails failed in the last recorded send"
        warnings.append(_sentence(failed, one, many))
    if cards is not None and emailed.scorecards_hash != cards.fingerprint:
        warnings.append(
            "Saved scorecards differ from the last scorecard email record."
            if cards.saved
            else "Current school scorecards differ from the last email record."
        )
    return warnings
