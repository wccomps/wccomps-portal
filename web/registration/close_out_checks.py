"""What a close-out would freeze unfinished, shown before confirming (never blocking), and the record of the
scorecards Email Scorecards sent, which those checks compare with the scores."""

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass

from orange_team.models import OrangeAssignment
from scoring.calculator import Standing
from scoring.models import IncidentReport, InjectScore, OrangeTeamScore, RedTeamScore

from ticketing.models import Ticket

from .models import Event
from .services import CloseOutCards, current_snapshots
from .stage import ScorecardsEmailed


@dataclass(frozen=True)
class CloseOutWarning:
    """A nonblocking notice and the related page."""

    text: str
    url_name: str


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
    if event is not None and cards is not None:
        inactive = event.team_assignments.filter(team__is_active=False).exclude(school_id__in=cards.scores)
        slots = inactive.order_by("team__team_number").values_list("team__team_number", flat=True)
        if numbers := ", ".join(map(str, slots)):
            warnings.append(CloseOutWarning(
                f"Inactive slots {numbers} have no card in the set being archived. The close-time roster and "
                "packet delivery/download summaries will be in the ZIP; raw service inputs are retained separately. "
                "The email audit cannot identify past card recipients.",
                "scoring:export_index",
            ))
    return warnings + [CloseOutWarning(text, "scoring:email_scorecards") for text in _scorecard_warnings(event, cards)]


def scorecards_fingerprint(event: Event | None, standings: list[Standing] | None = None) -> str:
    """A hash of every school's scorecard from standings (by default, computed now)."""
    scorecards = {
        school_id: (snapshot.standing.team.team_number, snapshot.scorecard)
        for school_id, snapshot in current_snapshots(event, standings).items()
    }
    return _card_fingerprint(scorecards)


def _card_fingerprint(scorecards: dict[int, tuple[int, dict[str, object]]]) -> str:
    # Round-trip so int keys become strings: sort_keys raises on a dict mixing int and str keys
    normalized = json.dumps(json.loads(json.dumps(scorecards)), sort_keys=True)
    return hashlib.sha256(normalized.encode()).hexdigest()


def _scorecard_warnings(event: Event | None, cards: CloseOutCards | None) -> list[str]:
    """Warnings about the last Email Scorecards for the event: none sent, failures, or scores changed since."""
    if event is None:
        return ["No event is open, so scorecard emails can't be checked: make sure the scorecards went out."]
    emailed = ScorecardsEmailed.latest(event)
    if emailed is None:
        return ["No scorecard email is recorded for this event. If you already emailed them, you can close out."]
    warnings = []
    if failed := emailed.failed:
        one, many = "scorecard email failed in the last send", "scorecard emails failed in the last send"
        warnings.append(_sentence(failed, one, many))
    if cards is not None and emailed.scorecards_hash != _card_fingerprint({
        school: (score.team_number, cards.snapshots[school]) for school, score in cards.scores.items()
    }):
        warnings.append(
            "Saved scorecards differ from the last scorecard email record." if cards.saved
            else "Scores changed after the last scorecard email."
        )
    return warnings
