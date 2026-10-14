"""Where the event lifecycle stands: one reading the import, Close Out, Clear leftover data and the pages share."""

from dataclasses import dataclass
from enum import StrEnum

from django.db.models import Exists, OuterRef, QuerySet
from packets.models import packets_sent
from scoring.models import IncidentReport, InjectScore, OrangeTeamScore, RedTeamScore, ServiceDetail, ServiceScore

from core.competition_utils import live_competition_data_exists
from core.models import AuditLog, CompetitionConfig
from team.models import Team

from .models import Event

# The audit action Email Scorecards records, with the open event's id (or None) and the scorecards' fingerprint
SCORECARDS_EMAILED = "scorecards_emailed"


class Stage(StrEnum):
    IDLE = "idle"
    READY = "ready"
    PLAYED = "played"
    UNRECORDED = "unrecorded"
    LEFTOVERS = "leftovers"


# What each stage says about the event; {event} is the open event
STAGE_SENTENCES: dict[Stage, str] = {
    Stage.IDLE: "No event is open and there is no live data.",
    Stage.READY: "{event} is open. No team has scores yet.",
    Stage.PLAYED: "{event} is open and teams have scores.",
    Stage.UNRECORDED: "No event is open, but teams have scores that are not recorded at any event yet.",
    Stage.LEFTOVERS: "No event is open, but there is live data with no scores (Discord links, tickets or orange "
    "assignments) that is not recorded at any event.",
}


class Action(StrEnum):
    """The things an operator does to an event, in the order of an event."""

    IMPORT = "import"
    SEND_PACKETS = "packets"
    START = "start"
    STOP = "stop"
    FINISH = "finish"
    CLOSE_OUT = "close_out"
    CANCEL = "cancel"
    CLEAR = "clear"


@dataclass(frozen=True)
class ActionText:
    """How the pages tell the operator to take the action ({event} is the open event), and the name of the link to
    where it is taken."""

    instruction: str
    link_label: str


ACTIONS: dict[Action, ActionText] = {
    Action.IMPORT: ActionText("Import the next school list to open an event.", "Import the school list"),
    Action.SEND_PACKETS: ActionText(
        "Send the packets for {event}: upload one on the Packets page and press Distribute.", "Send packets"
    ),
    Action.START: ActionText(
        "Run the Readiness Check, then start the competition when it is time.", "Check readiness and start"
    ),
    Action.STOP: ActionText(
        "When the competition ends, stop it with Stop Competition on the Competition page.", "Stop the competition"
    ),
    Action.FINISH: ActionText(
        "Finish grading, then email every school its scorecard with Email Scorecards.", "Email scorecards"
    ),
    Action.CLOSE_OUT: ActionText(
        "Close out the event with Close Out Event on the Competition page; with no event open, it asks for the "
        "event's name, date and type.",
        "Close out the event",
    ),
    Action.CANCEL: ActionText(
        "To call the event off, use Cancel event on the competition Danger Zone page.", "Cancel the event"
    ),
    Action.CLEAR: ActionText(
        "Remove it with Clear leftover data on the competition Danger Zone page, then import the next school list.",
        "Clear leftover data",
    ),
}

# Each stage's actions in order. While the competition runs the next action is Stop; otherwise it is the first of the
# stage's actions that isn't done, or its last.
STAGE_ACTIONS: dict[Stage, tuple[Action, ...]] = {
    Stage.IDLE: (Action.IMPORT,),
    Stage.READY: (Action.SEND_PACKETS, Action.START),
    Stage.PLAYED: (Action.FINISH, Action.CLOSE_OUT),
    Stage.UNRECORDED: (Action.FINISH, Action.CLOSE_OUT),
    Stage.LEFTOVERS: (Action.CLEAR,),
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
    Discord links, ...), whether any event was closed out before, whether a packet went out for the open event, and
    whether the scorecards were emailed for the scores now live."""

    stage: Stage
    event: Event | None
    running: bool
    live_data: bool
    closed_out_before: bool
    packets_sent: bool
    scorecards_emailed: bool

    @property
    def status(self) -> str:
        sentence = STAGE_SENTENCES[self.stage].format(event=self.event)
        return f"{sentence} The competition is running." if self.running else sentence

    def done(self, action: Action) -> bool:
        """Whether the stage's action is done; only the actions a stage moves past have a record of it."""
        if action == Action.SEND_PACKETS:
            return self.packets_sent
        if action == Action.FINISH:
            return self.scorecards_emailed
        return False

    @property
    def next_action(self) -> Action:
        if self.running:
            return Action.STOP
        actions = STAGE_ACTIONS[self.stage]
        return next((action for action in actions[:-1] if not self.done(action)), actions[-1])

    def instruction(self, action: Action) -> str:
        return ACTIONS[action].instruction.format(event=self.event)

    @property
    def next_step(self) -> str:
        return self.instruction(self.next_action)

    @property
    def steps_left(self) -> str:
        """The next step and the stage's actions after it."""
        if self.running:
            return self.next_step
        actions = STAGE_ACTIONS[self.stage]
        return " ".join(self.instruction(action) for action in actions[actions.index(self.next_action) :])


def _scorecards_emailed(event: Event | None) -> bool:
    """Whether Email Scorecards ran for the live scores: for the open event, or with none open, since the live data
    was last cleared."""
    sends = AuditLog.objects.filter(action=SCORECARDS_EMAILED)
    if event is not None:
        return sends.filter(details__event=event.pk).exists()
    sends = sends.filter(details__event=None)
    cleared = AuditLog.objects.filter(action__in=("event_closed_out", "leftover_data_cleared")).order_by("-created_at")
    if (last := cleared.first()) is not None:
        sends = sends.filter(created_at__gt=last.created_at)
    return sends.exists()


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
        packets_sent=packets_sent(event),
        scorecards_emailed=scores and _scorecards_emailed(event),
    )
