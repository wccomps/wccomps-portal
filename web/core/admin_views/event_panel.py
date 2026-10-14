"""The competition page's event card and the Running an Event guide: the steps of an event, where each is taken,
and which are done and which is current, read from the stage (registration.stage owns the lifecycle)."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from django.urls import reverse
from packets.models import Packet
from packets.services import teams_without_delivered_copy
from registration.stage import ACTIONS, Action, EventStage

from core.models import CompetitionConfig
from team.models import DiscordLink, SchoolInfo


class Section(StrEnum):
    """A section of the competition page, shown only while its action is next; the value is its id."""

    READINESS = "readiness"
    LIVE = "live"
    CLOSE_OUT = "close-out"
    LEFTOVERS = "leftovers"


class SetupSection(StrEnum):
    """A section of the Competition setup page, always shown; the value is its id."""

    SCHEDULE = "schedule"
    START_MESSAGE = "start-message"
    TEAM_ACCESS = "team-access"
    SCORING_ENGINE = "scoring-engine"
    READINESS = "readiness"


def setup_url(section: SetupSection) -> str:
    return f"{reverse('admin_competition_setup')}#{section}"


@dataclass(frozen=True)
class Place:
    """Where an action is taken, and what the competition page shows while it is the next action."""

    url_name: str
    # The competition page section the action is taken in; it exists only while the action is next
    section: Section | None = None
    # Where the action can also be taken while that section isn't shown
    elsewhere: SetupSection | None = None
    # The section the competition page shows while the action is next
    shows: Section | None = None
    # Taken by the next-step button itself rather than at a link
    by_next_step_button: bool = False


PLACES: dict[Action, Place] = {
    Action.IMPORT: Place("school_info_import"),
    Action.SEND_PACKETS: Place("packets_list"),
    Action.START: Place(
        "admin_competition", Section.READINESS, elsewhere=SetupSection.READINESS, shows=Section.READINESS
    ),
    Action.STOP: Place("admin_competition", shows=Section.LIVE, by_next_step_button=True),
    Action.FINISH: Place("scoring:email_scorecards", shows=Section.CLOSE_OUT),
    Action.CLOSE_OUT: Place("admin_competition", Section.CLOSE_OUT, shows=Section.CLOSE_OUT),
    Action.CANCEL: Place("admin_competition_danger"),
    Action.CLEAR: Place("admin_competition_danger", shows=Section.LEFTOVERS),
}


def action_url(action: Action, shown: Section | None = None) -> str:
    """Where to take the action, given the section the competition page shows now (None: a page with no stage, like
    the guide). A link never points at a section the page isn't showing."""
    place = PLACES[action]
    if place.section is not None and place.section == shown:
        return f"{reverse(place.url_name)}#{place.section}"
    if place.elsewhere is not None:
        return setup_url(place.elsewhere)
    return reverse(place.url_name)


class StepState(StrEnum):
    DONE = "done"
    CURRENT = "current"
    NEXT = "next"  # One of the stage's actions after the current one
    UPCOMING = "upcoming"


# Each state's word and badge variant on the panel
STATE_SHOWN: dict[StepState, tuple[str, str]] = {
    StepState.DONE: ("Done", "success"),
    StepState.CURRENT: ("Now", "info"),
    StepState.NEXT: ("Next", "neutral"),
    StepState.UPCOMING: ("Later", "neutral"),
}


class EventStatus(StrEnum):
    RUNNING = "running"
    STOPPED = "stopped"  # Teams have scores and the competition isn't running
    NOT_STARTED = "not_started"  # An event is open and no team has scores yet
    NO_EVENT = "no_event"


# Each status's word and badge variant on the event card
STATUS_SHOWN: dict[EventStatus, tuple[str, str]] = {
    EventStatus.RUNNING: ("Running", "success"),
    EventStatus.STOPPED: ("Stopped", "warning"),
    EventStatus.NOT_STARTED: ("Not started", "info"),
    EventStatus.NO_EVENT: ("No event", "neutral"),
}


def _status(stage: EventStage) -> EventStatus:
    if stage.running:
        return EventStatus.RUNNING
    if stage.event is None:
        return EventStatus.NO_EVENT
    return EventStatus.STOPPED if stage.played else EventStatus.NOT_STARTED


@dataclass(frozen=True)
class EventFacts:
    """The stage, and the counts and schedule the event card shows beyond it."""

    stage: EventStage
    schools: int
    draft_packets: int
    teams_without_copy: int  # Teams on the list missing a delivered copy of a packet sent for the open event
    members_linked: int = 0  # Discord accounts linked to a team
    starts_at: datetime | None = None  # The scheduled start and stop
    ends_at: datetime | None = None


def _plural(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def _import_detail(facts: EventFacts) -> str:
    # With an event open, the panel's event line counts the schools
    if not facts.schools or facts.stage.event is not None:
        return ""
    schools = _plural(facts.schools, "school is", "schools are")
    return f"{schools} on the list from the last import; the next import replaces it."


def _packets_detail(facts: EventFacts) -> str:
    lines = []
    if facts.draft_packets:
        lines.append(f"{_plural(facts.draft_packets, 'draft packet is', 'draft packets are')} not sent.")
    if facts.teams_without_copy:
        teams = _plural(facts.teams_without_copy, "team on the list has", "teams on the list have")
        lines.append(
            f"{teams} no delivered copy of a sent packet: on the packet's page, use Send to Teams Without It for "
            "new teams, or Resend Failed and Retry Pending."
        )
    return " ".join(lines)


@dataclass(frozen=True)
class Step:
    """One step of running an event: the action it takes and when it is done. It is current when its action is the
    stage's next one."""

    action: Action
    label: str
    done: Callable[[EventStage], bool]
    detail: Callable[[EventFacts], str] = lambda facts: ""

    @property
    def key(self) -> str:
        return self.action.value

    @property
    def link_label(self) -> str:
        return ACTIONS[self.action].link_label


STEPS: tuple[Step, ...] = (
    Step(
        Action.IMPORT,
        "Import the school list",
        done=lambda stage: stage.event is not None,
        detail=_import_detail,
    ),
    Step(
        Action.SEND_PACKETS,
        "Send packets",
        done=lambda stage: stage.packets_sent,
        detail=_packets_detail,
    ),
    Step(
        Action.START,
        "Check readiness and start the competition",
        done=lambda stage: stage.running or stage.played,
    ),
    Step(
        Action.STOP,
        "Stop the competition",
        done=lambda stage: not stage.running and stage.played,
    ),
    Step(
        Action.FINISH,
        "Finish scoring and email scorecards",
        done=lambda stage: stage.scorecards_emailed,
    ),
    Step(
        Action.CLOSE_OUT,
        "Close Out Event",
        done=lambda stage: False,
    ),
)

# The actions outside the normal run of an event, as the guide titles them
RARE_ACTIONS: tuple[tuple[Action, str], ...] = ((Action.CANCEL, "Cancel event"), (Action.CLEAR, "Clear leftover data"))


@dataclass(frozen=True)
class ShownStep:
    step: Step
    state: StepState
    detail: str
    url: str  # Where the step is taken, given the section the page shows

    @property
    def label(self) -> str:
        return STATE_SHOWN[self.state][0]

    @property
    def badge(self) -> str:
        return STATE_SHOWN[self.state][1]

    @property
    def is_current(self) -> bool:
        return self.state == StepState.CURRENT


@dataclass(frozen=True)
class EventPanel:
    """What the competition page's event card shows, and the one section below it."""

    facts: EventFacts
    steps: list[ShownStep]

    @property
    def stage(self) -> EventStage:
        return self.facts.stage

    @property
    def section(self) -> Section | None:
        return PLACES[self.stage.next_action].shows

    @property
    def status_word(self) -> str:
        return STATUS_SHOWN[_status(self.stage)][0]

    @property
    def status_badge(self) -> str:
        return STATUS_SHOWN[_status(self.stage)][1]

    @property
    def next_step_is_a_button(self) -> bool:
        return PLACES[self.stage.next_action].by_next_step_button

    @property
    def notes(self) -> list[ShownStep]:
        """The steps with something to say beyond their line (schools on the list, packets not delivered)."""
        return [row for row in self.steps if row.detail]

    @property
    def seasons_url(self) -> str:
        return reverse("registration_season_list")

    @property
    def action_url(self) -> str:
        return action_url(self.stage.next_action, self.section)

    @property
    def action_label(self) -> str:
        return ACTIONS[self.stage.next_action].link_label


def _state(step: Step, stage: EventStage) -> StepState:
    if step.action == stage.next_action:
        return StepState.CURRENT
    if step.done(stage):
        return StepState.DONE
    return StepState.NEXT if step.action in stage.actions_after_next else StepState.UPCOMING


def event_panel(facts: EventFacts) -> EventPanel:
    shown = PLACES[facts.stage.next_action].shows
    steps = [
        ShownStep(step, _state(step, facts.stage), step.detail(facts), action_url(step.action, shown)) for step in STEPS
    ]
    return EventPanel(facts, steps)


def event_facts(stage: EventStage) -> EventFacts:
    """The counts and schedule the event card shows, from a fixed number of queries."""
    teams_without_copy = 0
    if stage.packets_sent:
        teams_without_copy = teams_without_delivered_copy(Packet.objects.sent().filter(event=stage.event)).count()
    config = CompetitionConfig.get_config()
    return EventFacts(
        stage=stage,
        schools=SchoolInfo.objects.count(),
        draft_packets=Packet.objects.filter(status="draft").count(),
        teams_without_copy=teams_without_copy,
        members_linked=DiscordLink.objects.filter(is_active=True, team__isnull=False).count(),
        starts_at=config.competition_start_time,
        ends_at=config.competition_end_time,
    )
