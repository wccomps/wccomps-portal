"""Where each event action is taken, and the sections of the competition pages that links point at.

registration.stage owns the lifecycle (which action is next, which is done); this module says where the portal
takes each action and how it names it, for the competition page's event card, the guide and other pages' links.
"""

from dataclasses import dataclass
from enum import StrEnum

from django.urls import reverse
from registration.stage import Action


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
    PAGE = "page"  # On another page, or the top of the competition page: target is its URL name
    SECTION = "section"  # In a competition page section, there only while shown: target is the Section
    CONTROLS = "controls"  # At the competition page's Start/Stop row


@dataclass(frozen=True)
class ActionPlace:
    """How the pages name an action and where it is taken."""

    heading: str
    kind: Kind
    target: str = ""
    # The section the competition page shows while this is the next action
    shows: Section | None = None
    # Outside the normal run of an event: not one of the event card's steps
    rare: bool = False


# In the order of an event (Action's order)
PLACES: dict[Action, ActionPlace] = {
    Action.IMPORT: ActionPlace("Import the school list", Kind.PAGE, "school_info_import"),
    Action.SEND_PACKETS: ActionPlace("Send packets", Kind.PAGE, "packets_list"),
    Action.START: ActionPlace("Check readiness and start the competition", Kind.CONTROLS, shows=Section.READINESS),
    # While it runs Stop is the next-step button at the top of the competition page
    Action.STOP: ActionPlace("Stop the competition", Kind.PAGE, "admin_competition", shows=Section.LIVE),
    Action.FINISH: ActionPlace(
        "Finish scoring and email scorecards", Kind.PAGE, "scoring:email_scorecards", shows=Section.CLOSE_OUT
    ),
    Action.CLOSE_OUT: ActionPlace("Close Out Event", Kind.SECTION, Section.CLOSE_OUT, shows=Section.CLOSE_OUT),
    Action.CANCEL: ActionPlace("Cancel event", Kind.PAGE, "admin_competition_danger", rare=True),
    Action.CLEAR: ActionPlace(
        "Clear leftover data", Kind.PAGE, "admin_competition_danger", shows=Section.LEFTOVERS, rare=True
    ),
}

# The event card's steps, in order
STEPS: tuple[Action, ...] = tuple(action for action, place in PLACES.items() if not place.rare)


def action_url(action: Action, shown: Section | None = None) -> str:
    """Where to take the action, given the section the competition page shows now (None: a page with no stage, like
    the guide). A link never points at a section the page isn't showing."""
    place = PLACES[action]
    if place.kind == Kind.CONTROLS:
        return f"{reverse('admin_competition')}#{CONTROLS}"
    if place.kind == Kind.SECTION:
        return reverse("admin_competition") + (f"#{place.target}" if place.target == shown else "")
    return reverse(place.target)


def setup_url(section: SetupSection) -> str:
    return f"{reverse('admin_competition_setup')}#{section}"


def section_partial(section: Section) -> str:
    return f"admin/competition/{section}.html"


def ids(sections: type[StrEnum]) -> dict[str, str]:
    """A section enum for templates, which would call the class: NAME -> id."""
    return {section.name: section.value for section in sections}
