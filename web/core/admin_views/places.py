"""Where each event action is taken, and the sections of the competition pages that links point at.

registration.stage owns the lifecycle (which action is next, which is done); this module says where the portal
takes each action and how it names it, for the competition page's Event and Steps sections, the guide and other
pages' links.
"""

from dataclasses import dataclass
from enum import StrEnum

from django.urls import reverse
from registration.stage import Action

from core.page_titles import PAGE_TITLES


class Section(StrEnum):
    """A section of the competition page, shown while the next action's place says so; the value is its id."""

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


# The competition page's Start/Stop row, always shown
CONTROLS = "controls"


class Kind(StrEnum):
    ELSEWHERE = "elsewhere"  # On another page: target is its URL name
    # On the competition page: target is the id of the always-shown part it is taken at, or empty when it is taken in
    # a part shown only while it is next (a link there carries no anchor, so it never lands on a missing part)
    HERE = "here"


@dataclass(frozen=True)
class ActionPlace:
    """How the pages name an action and where it is taken."""

    heading: str
    kind: Kind
    target: str = ""
    # The section the competition page shows while this is the next action
    shows: Section | None = None
    # Outside the normal run of an event: not one of the competition page's steps
    rare: bool = False

    @property
    def elsewhere(self) -> bool:
        return self.kind == Kind.ELSEWHERE

    @property
    def page_title(self) -> str:
        """The title of the other page it is taken on, as that page's header shows it."""
        return PAGE_TITLES[self.target] if self.elsewhere else ""


# In the order of an event (Action's order)
PLACES: dict[Action, ActionPlace] = {
    Action.IMPORT: ActionPlace("Import the school list", Kind.ELSEWHERE, "school_info_import"),
    Action.SEND_PACKETS: ActionPlace("Send packets", Kind.ELSEWHERE, "packets_list"),
    Action.START: ActionPlace("Check readiness and start the competition", Kind.HERE, CONTROLS, Section.READINESS),
    # While it runs Stop is the next-step button at the top of the competition page
    Action.STOP: ActionPlace("Stop the competition", Kind.HERE, shows=Section.LIVE),
    Action.FINISH: ActionPlace(
        "Finish scoring and email scorecards", Kind.ELSEWHERE, "scoring:email_scorecards", Section.CLOSE_OUT
    ),
    Action.CLOSE_OUT: ActionPlace("Close Out Event", Kind.HERE, shows=Section.CLOSE_OUT),
    Action.CANCEL: ActionPlace("Cancel event", Kind.ELSEWHERE, "admin_competition_danger", rare=True),
    Action.CLEAR: ActionPlace(
        "Clear leftover data", Kind.ELSEWHERE, "admin_competition_danger", Section.LEFTOVERS, rare=True
    ),
}

# The competition page's steps, in order
STEPS: tuple[Action, ...] = tuple(action for action, place in PLACES.items() if not place.rare)


def action_url(action: Action) -> str:
    """Where to take the action."""
    place = PLACES[action]
    if place.elsewhere:
        return reverse(place.target)
    return reverse("admin_competition") + (f"#{place.target}" if place.target else "")


def setup_url(section: SetupSection) -> str:
    return f"{reverse('admin_competition_setup')}#{section}"


def section_partial(section: Section) -> str:
    return f"admin/competition/{section}.html"


def ids(sections: type[StrEnum]) -> dict[str, str]:
    """A section enum for templates, which would call the class: NAME -> id."""
    return {section.name: section.value for section in sections}
