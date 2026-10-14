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
    UPCOMING = "upcoming"


# Each state's word and badge variant on the panel
STATE_SHOWN: dict[StepState, tuple[str, str]] = {
    StepState.DONE: ("Done", "success"),
    StepState.CURRENT: ("Now", "info"),
    StepState.UPCOMING: ("Later", "neutral"),
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
    if not facts.schools:
        return ""
    schools = _plural(facts.schools, "school is", "schools are")
    if facts.stage.event is not None:
        return f"{schools} on the list."
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
    """One step of running an event: the action it takes, what it does, and when it is done. It is current when
    its action is the stage's next one."""

    action: Action
    label: str
    help: str
    done: Callable[[EventStage], bool]
    detail: Callable[[EventFacts], str] = lambda facts: ""
    # The Close Out checks (unfinished grading, scorecard emails, school problems) load on demand under this step
    close_out_checks: bool = False

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
        "Gives each school a team number and opens the event. Later registrations go in with Add schools.",
        done=lambda stage: stage.event is not None,
        detail=_import_detail,
    ),
    Step(
        Action.SEND_PACKETS,
        "Send packets",
        "Gives each school on the list the packet and its team's username and password: by email when the packet "
        "says so, and to download in the portal unless that is turned off.",
        done=lambda stage: stage.packets_sent,
        detail=_packets_detail,
    ),
    Step(
        Action.START,
        "Check readiness and start the competition",
        "Run the Readiness Check, then on competition day Start Competition turns on the active teams' accounts, "
        "opens the controlled apps to blue teams and posts the start message, if one is set. A start time set under "
        "Schedule starts it on its own.",
        done=lambda stage: stage.running or _played(stage),
    ),
    Step(
        Action.STOP,
        "Stop the competition",
        "At the end: turns every team account off and closes the controlled apps. Scores stay, so grading goes on. "
        "An end time set under Schedule stops it on its own.",
        done=lambda stage: not stage.running and _played(stage),
    ),
    Step(
        Action.FINISH,
        "Finish scoring and email scorecards",
        "White team finishes grading and the scoring reviews are approved, then Email Scorecards sends each school "
        "its scorecard. Sends made before the portal recorded them don't show. If you already emailed them, you can "
        "close out.",
        done=lambda stage: stage.scorecards_emailed,
        close_out_checks=True,
    ),
    Step(
        Action.CLOSE_OUT,
        "Close Out Event",
        "Saves every listed school's scorecard as its result for the event, then deletes the event's scores, "
        "tickets and team Discord links, ready for the next event.",
        done=lambda stage: False,
        close_out_checks=True,
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

    @property
    def shows_close_out_checks(self) -> bool:
        return self.step.close_out_checks and self.is_current


@dataclass(frozen=True)
class EventPanel:
    """What the panel shows: the event, the stage (its sentence and next step), where the next step is taken, and the
    steps."""

    stage: EventStage
    steps: list[ShownStep]

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
    return StepState.DONE if step.done(stage) else StepState.UPCOMING


def event_panel(facts: EventFacts) -> EventPanel:
    return EventPanel(facts.stage, [ShownStep(step, _state(step, facts.stage), step.detail(facts)) for step in STEPS])


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
