"""The competition page's Event panel and the Running an Event guide: the steps of an event, where each is taken,
and which are done and which is current, read from the stage (registration.stage owns the lifecycle)."""

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from django.urls import reverse
from packets.models import Packet
from packets.services import teams_without_delivered_copy
from registration.stage import ACTIONS, Action, EventStage, Stage

from team.models import SchoolInfo

# Where each action is taken: a page's URL name, and a section of it
PLACES: dict[Action, tuple[str, str]] = {
    Action.IMPORT: ("school_info_import", ""),
    Action.SEND_PACKETS: ("packets_list", ""),
    Action.START: ("admin_competition", "readiness"),
    Action.STOP: ("admin_competition", "controls"),
    Action.FINISH: ("scoring:email_scorecards", ""),
    Action.CLOSE_OUT: ("admin_competition", "close-out"),
    Action.CANCEL: ("admin_competition_danger", ""),
    Action.CLEAR: ("admin_competition_danger", ""),
}


def action_url(action: Action) -> str:
    url_name, anchor = PLACES[action]
    return reverse(url_name) + (f"#{anchor}" if anchor else "")


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


class Section(StrEnum):
    """The one section the competition page shows below Start and Stop, for the next step."""

    READINESS = "readiness"
    RUNNING = "running"
    CLOSE_OUT = "close_out"
    LEFTOVERS = "leftovers"


SECTIONS: dict[Action, Section] = {
    Action.START: Section.READINESS,
    Action.STOP: Section.RUNNING,
    Action.FINISH: Section.CLOSE_OUT,
    Action.CLOSE_OUT: Section.CLOSE_OUT,
    Action.CLEAR: Section.LEFTOVERS,
}


@dataclass(frozen=True)
class EventFacts:
    """The stage, and the counts the steps show beyond it."""

    stage: EventStage
    schools: int
    draft_packets: int
    teams_without_copy: int  # Teams on the list missing a delivered copy of a packet sent for the open event


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


def _played(stage: EventStage) -> bool:
    return stage.stage in (Stage.PLAYED, Stage.UNRECORDED)


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
    def url(self) -> str:
        return action_url(self.action)

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
        done=lambda stage: stage.running or _played(stage),
    ),
    Step(
        Action.STOP,
        "Stop the competition",
        done=lambda stage: not stage.running and _played(stage),
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
    """What the panel shows: the event and its schools, the stage (its sentence and next step), where the next step
    is taken, the steps, and the page's section for the next step."""

    stage: EventStage
    schools: int
    steps: list[ShownStep]

    @property
    def section(self) -> Section | None:
        return SECTIONS.get(self.stage.next_action)

    @property
    def notes(self) -> list[ShownStep]:
        """The steps with something to say beyond their line (schools on the list, packets not delivered)."""
        return [row for row in self.steps if row.detail]

    @property
    def seasons_url(self) -> str:
        return reverse("registration_season_list")

    @property
    def action_url(self) -> str:
        return action_url(self.stage.next_action)

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
    steps = [ShownStep(step, _state(step, facts.stage), step.detail(facts)) for step in STEPS]
    return EventPanel(facts.stage, facts.schools, steps)


def event_facts(stage: EventStage) -> EventFacts:
    """The counts the steps show: a few queries, none growing with the number of teams."""
    teams_without_copy = 0
    if stage.packets_sent:
        teams_without_copy = teams_without_delivered_copy(Packet.objects.sent().filter(event=stage.event)).count()
    return EventFacts(
        stage=stage,
        schools=SchoolInfo.objects.count(),
        draft_packets=Packet.objects.filter(status="draft").count(),
        teams_without_copy=teams_without_copy,
    )
