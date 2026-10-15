"""The competition page's event panel: the event, its status, the next step and the steps, which are done and which
is current, read from the stage (registration.stage owns the lifecycle; places says where each action is taken)."""

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


# Each state's word and badge variant on the event panel
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


# Each status's word and badge variant on the event panel
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
    """The stage, and the counts and schedule the event panel shows beyond it."""

    stage: EventStage
    schools: int
    draft_packets: int
    teams_without_copy: int  # Teams on the list missing a delivered copy of a packet sent for the open event
    members_linked: int  # Discord accounts linked to a team
    starts_at: datetime | None  # The scheduled start, while the schedule will still start it
    ends_at: datetime | None  # The scheduled stop


def _plural(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def _import_detail(facts: EventFacts) -> str:
    # With an event open, the event panel's Schools row counts them
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
    url: str  # Where the step is taken, given the section the page shows

    @property
    def heading(self) -> str:
        return PLACES[self.action].heading

    @property
    def page(self) -> str:
        return PLACES[self.action].page

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
    """What the competition page's event panel shows, and the one section below it."""

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
    def notes(self) -> list[ShownStep]:
        """The steps with something to say beyond their line (schools on the list, packets not delivered)."""
        return [row for row in self.steps if row.detail]

    @property
    def seasons_url(self) -> str:
        return reverse("registration_season_list")

    @property
    def action_page(self) -> str:
        """The page the next action is taken on, when it isn't this one (whose controls need no link)."""
        return PLACES[self.stage.next_action].page

    @property
    def action_url(self) -> str:
        return action_url(self.stage.next_action, self.section)

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
    shown = PLACES[facts.stage.next_action].shows
    steps = [
        ShownStep(
            action,
            _state(action, facts.stage),
            DETAILS[action](facts) if action in DETAILS else "",
            action_url(action, shown),
        )
        for action in STEPS
    ]
    return EventPanel(facts, steps)


def event_facts(stage: EventStage) -> EventFacts:
    """The counts and schedule the event panel shows, from a fixed number of queries."""
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
    )
