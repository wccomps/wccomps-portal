"""The competition page's Event and Steps sections: the event, its status, the next step and the steps, which are
done and which is current, read from the stage (registration.stage owns the lifecycle; places says where each action
is taken)."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from django.urls import reverse
from packets.models import Packet
from packets.services import teams_without_delivered_copy
from registration.stage import ACTIONS, Action, EventStage

from core.admin_views.places import PLACES, STEPS, Section, action_url
from core.models import CompetitionConfig
from team.models import DiscordLink, SchoolInfo


class StepState(StrEnum):
    DONE = "done"
    CURRENT = "current"
    NEXT = "next"  # One of the stage's actions after the current one
    UPCOMING = "upcoming"


# Each step state's word and badge variant in the Steps section
STATE_SHOWN: dict[StepState, tuple[str, str]] = {
    StepState.DONE: ("Done", "success"),
    StepState.CURRENT: ("Now", "info"),
    StepState.NEXT: ("Next", "neutral"),
    StepState.UPCOMING: ("Later", "neutral"),
}


class EventStatus(StrEnum):
    RUNNING = "running"
    STOPPED = "stopped"  # An event is open, teams have scores and the competition isn't running
    NOT_STARTED = "not_started"  # An event is open and no team has scores yet
    NO_EVENT = "no_event"  # No event is open and the competition isn't running, whether or not teams have scores


# Each status's word and badge variant in the Event section
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
    """The stage, and the counts and schedule the Event and Steps sections show beyond it."""

    stage: EventStage
    schools: int
    draft_packets: int
    teams_without_copy: int  # Teams on the list missing a delivered copy of a packet sent for the open event
    members_linked: int  # Discord accounts linked to a team
    starts_at: datetime | None  # The scheduled start, while the schedule will still start it
    ends_at: datetime | None  # The scheduled stop
    last_run_incomplete: bool


def _plural(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def _import_detail(facts: EventFacts) -> str:
    # With an event open, the Event section counts the schools
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


# What a step says beyond its line
DETAILS: dict[Action, Callable[[EventFacts], str]] = {
    Action.IMPORT: _import_detail,
    Action.SEND_PACKETS: _packets_detail,
}


@dataclass(frozen=True)
class ShownStep:
    action: Action
    state: StepState
    detail: str

    @property
    def heading(self) -> str:
        return PLACES[self.action].heading

    @property
    def elsewhere(self) -> bool:
        return PLACES[self.action].elsewhere

    @property
    def page_title(self) -> str:
        return PLACES[self.action].page_title

    @property
    def url(self) -> str:
        return action_url(self.action)

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
    """What the competition page's Event and Steps sections show, and the one section below them."""

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
    def seasons_url(self) -> str:
        return reverse("registration_season_list")

    @property
    def start_offered(self) -> bool:
        """Start while stopped, or again while running after a start that left something off."""
        return not self.stage.running or self.facts.last_run_incomplete

    @property
    def stop_offered(self) -> bool:
        """Stop while running, or again while stopped after a stop that left something on."""
        return self.stage.running or self.facts.last_run_incomplete

    @property
    def action_elsewhere(self) -> bool:
        """Whether the next action is taken on another page; this page's own controls need no link."""
        return PLACES[self.stage.next_action].elsewhere

    @property
    def advanced_open(self) -> bool:
        """Whether the next action is taken under Advanced, which then shows open."""
        return PLACES[self.stage.next_action].in_advanced

    @property
    def action_url(self) -> str:
        return action_url(self.stage.next_action)

    @property
    def action_label(self) -> str:
        return ACTIONS[self.stage.next_action].link_label


def _state(action: Action, stage: EventStage) -> StepState:
    if action == stage.next_action:
        return StepState.CURRENT
    if stage.done(action):
        return StepState.DONE
    return StepState.NEXT if action in stage.actions_after_next else StepState.UPCOMING


def event_panel(facts: EventFacts) -> EventPanel:
    steps = [
        ShownStep(action, _state(action, facts.stage), DETAILS[action](facts) if action in DETAILS else "")
        for action in STEPS
    ]
    return EventPanel(facts, steps)


def event_facts(stage: EventStage) -> EventFacts:
    """The counts and schedule the Event and Steps sections show, from a fixed number of queries."""
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
        starts_at=config.competition_start_time if config.schedule_will_start() else None,
        ends_at=config.competition_end_time,
        last_run_incomplete=config.last_run_incomplete,
    )
