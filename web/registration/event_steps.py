"""The steps of running an event, as the competition page's event panel shows them: each step's label, what it does,
where it is done, and when it is done or current, read from the stage (registration.stage) and a few cheap counts.
The panel's sentence and next step are the stage's (STAGE_TEXT); this module adds where to go."""

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from django.db.models import Exists, OuterRef
from django.urls import reverse
from packets.models import Packet, PacketDistribution

from team.models import SchoolInfo, Team

from .stage import EventStage, Stage


class StepState(StrEnum):
    DONE = "done"
    CURRENT = "current"
    UPCOMING = "upcoming"

    @property
    def label(self) -> str:
        return {"done": "Done", "current": "Now", "upcoming": "Later"}[self.value]

    @property
    def badge(self) -> str:
        return {"done": "success", "current": "info", "upcoming": "neutral"}[self.value]


@dataclass(frozen=True)
class Link:
    """Where a step is done: a page by URL name, and a section of it."""

    label: str
    url_name: str
    anchor: str = ""

    @property
    def url(self) -> str:
        return reverse(self.url_name) + (f"#{self.anchor}" if self.anchor else "")


@dataclass(frozen=True)
class EventFacts:
    """The stage, and the counts the steps read beyond it."""

    stage: EventStage
    schools: int
    packet_sent: bool  # A packet went out for the open event
    draft_packets: int
    teams_without_packet: int  # Teams on the list missing a packet sent for the open event


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
    if facts.teams_without_packet:
        teams = _plural(facts.teams_without_packet, "team on the list has", "teams on the list have")
        lines.append(f"{teams} no copy of a sent packet: use Send to Teams Without It on that packet's page.")
    return " ".join(lines)


def _played(facts: EventFacts) -> bool:
    return facts.stage.stage in (Stage.PLAYED, Stage.UNRECORDED)


def _stopped_in(*stages: Stage) -> Callable[[EventFacts], bool]:
    return lambda facts: not facts.stage.running and facts.stage.stage in stages


@dataclass(frozen=True)
class Step:
    """One step of running an event. A step is current when its rule says so and it isn't done."""

    key: str
    label: str
    help: str
    link: Link
    done: Callable[[EventFacts], bool]
    current: Callable[[EventFacts], bool]
    detail: Callable[[EventFacts], str] = lambda facts: ""
    # The Close Out checks (unfinished grading, scorecard emails) load on demand and show under this step
    close_out_checks: bool = False


STEPS: tuple[Step, ...] = (
    Step(
        key="import",
        label="Import the school list",
        help="Gives each school a team number and opens the event. Later registrations go in with Add schools.",
        link=Link("Import the school list", "school_info_import"),
        done=lambda facts: facts.stage.event is not None,
        current=_stopped_in(Stage.IDLE),
        detail=_import_detail,
    ),
    Step(
        key="packets",
        label="Send packets",
        help="Sends each school on the list the packet with its team's username and password (the password is set "
        "on the first send).",
        link=Link("Send packets", "packets_list"),
        done=lambda facts: facts.packet_sent,
        current=_stopped_in(Stage.READY),
        detail=_packets_detail,
    ),
    Step(
        key="readiness",
        label="Check readiness",
        help="Checks team accounts, Discord settings, packets, Quotient and that no old data is left, before the "
        "start.",
        link=Link("Check readiness", "admin_competition", "readiness"),
        done=lambda facts: facts.stage.running or _played(facts),
        current=_stopped_in(Stage.READY),
    ),
    Step(
        key="start",
        label="Start the competition",
        help="On competition day: turns on the active teams' accounts, opens the controlled apps to blue teams and "
        "posts the start message. A start time set under Schedule does this on its own.",
        link=Link("Start the competition", "admin_competition", "controls"),
        done=lambda facts: facts.stage.running or _played(facts),
        current=_stopped_in(Stage.READY),
    ),
    Step(
        key="stop",
        label="Stop the competition",
        help="At the end: turns every team account off and closes the controlled apps. Scores stay, so grading goes "
        "on. An end time set under Schedule does this on its own.",
        link=Link("Stop the competition", "admin_competition", "controls"),
        done=lambda facts: not facts.stage.running and _played(facts),
        current=lambda facts: facts.stage.running,
    ),
    Step(
        key="finish",
        label="Finish scoring and email scorecards",
        help="White team finishes grading and the scoring reviews are approved, then Email Scorecards sends each "
        "school its scorecard.",
        link=Link("Email scorecards", "scoring:email_scorecards"),
        done=lambda facts: False,
        current=_stopped_in(Stage.PLAYED),
        close_out_checks=True,
    ),
    Step(
        key="close_out",
        label="Close Out Event",
        help="Freezes every listed school's scorecard as its result for the season, then deletes the event's scores, "
        "tickets and team Discord links, ready for the next event.",
        link=Link("Close out the event", "admin_competition", "close-out"),
        done=lambda facts: False,
        current=_stopped_in(Stage.PLAYED, Stage.UNRECORDED),
    ),
)

# Where the panel sends a stage no step is current in
STAGE_LINKS: dict[Stage, Link] = {
    Stage.LEFTOVERS: Link("Clear leftover data", "admin_competition_danger"),
}


@dataclass(frozen=True)
class ShownStep:
    step: Step
    state: StepState
    detail: str
    close_out_checks: bool


@dataclass(frozen=True)
class EventPanel:
    """What the panel shows: the event, the stage's sentence and next step, where to go, and the steps."""

    stage: EventStage
    action: Link | None
    steps: list[ShownStep]

    @property
    def title(self) -> str:
        event = self.stage.event
        return f"{event.name}, season {event.season.name}" if event is not None else "No event open"

    @property
    def status(self) -> str:
        return self.stage.status

    @property
    def next_step(self) -> str:
        return self.stage.next_step


def _state(step: Step, facts: EventFacts) -> StepState:
    if step.done(facts):
        return StepState.DONE
    return StepState.CURRENT if step.current(facts) else StepState.UPCOMING


def event_panel(facts: EventFacts) -> EventPanel:
    steps = []
    for step in STEPS:
        state = _state(step, facts)
        current = state == StepState.CURRENT
        steps.append(ShownStep(step, state, step.detail(facts), step.close_out_checks and current))
    action = next((row.step.link for row in steps if row.state == StepState.CURRENT), None)
    return EventPanel(facts.stage, action or STAGE_LINKS.get(facts.stage.stage), steps)


def event_facts(stage: EventStage) -> EventFacts:
    """The counts the steps read: a few queries, none growing with the number of teams."""
    sent = Packet.objects.sent().filter(event=stage.event)
    packet_sent = stage.event is not None and sent.exists()
    teams_without_packet = 0
    if packet_sent:
        missing = sent.exclude(
            Exists(PacketDistribution.objects.filter(packet=OuterRef("pk"), team=OuterRef(OuterRef("pk"))))
        )
        # The teams a send reaches: active, on the school list
        teams_without_packet = (
            Team.objects.filter(is_active=True, school_info__isnull=False).filter(Exists(missing)).count()
        )
    return EventFacts(
        stage=stage,
        schools=SchoolInfo.objects.count(),
        packet_sent=packet_sent,
        draft_packets=Packet.objects.filter(status="draft").count(),
        teams_without_packet=teams_without_packet,
    )
