"""What a close-out would freeze unfinished, shown before confirming (never blocking), and the record of the
scorecards Email Scorecards sent, which those checks compare with the scores."""

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass

from orange_team.models import OrangeAssignment
from scoring.calculator import Standing
from scoring.models import IncidentReport, InjectScore, OrangeTeamScore, RedTeamScore

from core.models import AuditLog
from ticketing.models import Ticket

from .models import Event
from .services import current_snapshots

SCORECARDS_EMAILED = "scorecards_emailed"


@dataclass(frozen=True)
class CloseOutWarning:
    """A sentence about unfinished work, and the URL name of the page where it is finished."""

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


def close_out_warnings(event: Event | None) -> list[CloseOutWarning]:
    """Unfinished work a close-out of the event (None: the event it records unrecorded scores at) would freeze as it
    stands, and the state of the scorecard emails. They never block the close-out."""
    warnings = [
        CloseOutWarning(_sentence(count, work.one, work.many), work.url_name)
        for work in UNFINISHED_WORK
        if (count := work.count())
    ]
    return warnings + [CloseOutWarning(text, "scoring:email_scorecards") for text in _scorecard_warnings(event)]


def scorecards_fingerprint(event: Event | None, standings: list[Standing] | None = None) -> str:
    """A hash of every school's scorecard from standings (by default, computed now)."""
    scorecards = {
        school_id: (snapshot.standing.team.team_number, snapshot.scorecard)
        for school_id, snapshot in current_snapshots(event, standings).items()
    }
    # Round-trip so int keys become strings: sort_keys raises on a dict mixing int and str keys
    normalized = json.dumps(json.loads(json.dumps(scorecards)), sort_keys=True)
    return hashlib.sha256(normalized.encode()).hexdigest()


def record_scorecards_emailed(event: Event | None, fingerprint: str, sent: int, failed: int, actor: str) -> None:
    """Record an Email Scorecards send with the fingerprint of the scorecards it emailed (taken from the standings
    its emails were built from, before sending). A send with no event open is recorded for the audit trail only: it
    can't be matched to the event a later close-out records."""
    AuditLog.objects.create(
        action=SCORECARDS_EMAILED,
        admin_user=actor,
        target_entity="event",
        target_id=event.pk if event else 0,
        details={"event": event.pk if event else None, "teams": sent, "failed": failed, "scorecards_hash": fingerprint},
    )


def _scorecard_warnings(event: Event | None) -> list[str]:
    """Warnings about the last Email Scorecards for the event: none sent, failures, or scores changed since."""
    if event is None:
        return ["No event is open, so scorecard emails can't be checked: make sure the scorecards went out."]
    sends = AuditLog.objects.filter(action=SCORECARDS_EMAILED, details__event=event.pk)
    emailed = sends.order_by("-created_at", "-pk").first()
    if emailed is None:
        return ["Scorecards haven't been emailed for this event."]
    warnings = []
    if failed := emailed.details.get("failed", 0):
        one, many = "scorecard email failed in the last send", "scorecard emails failed in the last send"
        warnings.append(_sentence(failed, one, many))
    if emailed.details.get("scorecards_hash") != scorecards_fingerprint(event):
        warnings.append("Scores changed after the last scorecard email.")
    return warnings
