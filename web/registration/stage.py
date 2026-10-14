"""Where the event lifecycle stands: one reading the import, Close Out, Clear leftover data and the pages share."""

from dataclasses import dataclass
from enum import StrEnum

from django.db.models import Exists, OuterRef, QuerySet
from scoring.models import IncidentReport, InjectScore, OrangeTeamScore, RedTeamScore, ServiceDetail, ServiceScore

from core.competition_utils import live_competition_data_exists
from core.models import CompetitionConfig
from team.models import Team

from .models import Event


class Stage(StrEnum):
    IDLE = "idle"
    READY = "ready"
    PLAYED = "played"
    UNRECORDED = "unrecorded"
    LEFTOVERS = "leftovers"


# Each stage's sentence and next step; {event} is the open event
STAGE_TEXT: dict[Stage, tuple[str, str]] = {
    Stage.IDLE: (
        "No event is open and there is no live data.",
        "Import the next school list to open an event.",
    ),
    Stage.READY: (
        "{event} is open. No team has scores yet.",
        "Start the competition when it is time.",
    ),
    Stage.PLAYED: (
        "{event} is open and teams have scores.",
        "Once grading is done and the scorecards are emailed, close out the event with Close Out Event.",
    ),
    Stage.UNRECORDED: (
        "No event is open, but teams have scores that are not recorded at any event yet.",
        "Record them with Close Out Event on the Competition page: give the event a name, a date and a type.",
    ),
    Stage.LEFTOVERS: (
        "No event is open, but there is live data with no scores (Discord links, tickets or orange assignments) that "
        "is not recorded at any event.",
        "Remove it with Clear leftover data on the competition Danger Zone page, then import the next school list.",
    ),
}


def scored_teams() -> QuerySet[Team]:
    """Teams with any live score row: what a close-out freezes and then deletes for them."""
    team = OuterRef("pk")
    return Team.objects.filter(
        Exists(ServiceScore.objects.filter(team=team))
        | Exists(ServiceDetail.objects.filter(team=team))
        | Exists(InjectScore.objects.filter(team=team))
        | Exists(OrangeTeamScore.objects.filter(team=team))
        | Exists(IncidentReport.objects.filter(team=team))
        | Exists(RedTeamScore.objects.filter(affected_teams=team))
    )


@dataclass(frozen=True)
class EventStage:
    """The stage, the open event, whether the competition runs, whether there is any live data (scores, tickets,
    Discord links, ...) and whether any event was closed out before."""

    stage: Stage
    event: Event | None
    running: bool
    live_data: bool
    closed_out_before: bool

    @property
    def status(self) -> str:
        sentence = STAGE_TEXT[self.stage][0].format(event=self.event)
        return f"{sentence} The competition is running." if self.running else sentence

    @property
    def next_step(self) -> str:
        return "Stop the competition first." if self.running else STAGE_TEXT[self.stage][1]


def event_stage() -> EventStage:
    """Read the stage now. Writers read it again under the school list lock (and the locked config row)."""
    event = Event.objects.open()
    scores = scored_teams().exists()
    live_data = scores or live_competition_data_exists()
    if event is not None:
        stage = Stage.PLAYED if scores else Stage.READY
    elif scores:
        stage = Stage.UNRECORDED
    else:
        stage = Stage.LEFTOVERS if live_data else Stage.IDLE
    return EventStage(
        stage=stage,
        event=event,
        running=CompetitionConfig.get_config().applications_enabled,
        live_data=live_data,
        closed_out_before=Event.objects.filter(is_finalized=True).exists(),
    )
