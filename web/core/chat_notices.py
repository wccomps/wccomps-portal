"""What the chat notices say, whichever chat backend shows them: Discord renders a Card as an embed, Zulip as Markdown
lines. Text in them is as the portal stored it; each backend makes it safe for its chat (Zulip silences mentions)."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import NamedTuple

from core.models import CompetitionConfig
from core.queued_tasks import PostTicketUpdate
from team.models import MAX_TEAMS
from ticketing.models import Ticket


class Field(NamedTuple):
    """A named value, left out when blank. A time is shown in each reader's own timezone. wide: long text, which
    Discord shows on a row of its own."""

    name: str
    value: str | datetime | None
    wide: bool = False


@dataclass(frozen=True)
class Card:
    """Named values under a title, with a headline beside it when there is one."""

    title: str
    fields: list[Field]
    headline: str = ""


class CompetitionStatus(StrEnum):
    RUNNING = "RUNNING"
    SCHEDULED = "SCHEDULED"
    STOPPED = "STOPPED"


def competition_status(config: CompetitionConfig) -> CompetitionStatus:
    if config.applications_enabled:
        return CompetitionStatus.RUNNING
    if config.schedule_will_start():
        return CompetitionStatus.SCHEDULED
    return CompetitionStatus.STOPPED


def status_card(config: CompetitionConfig) -> Card:
    """The competition's status, as the chat status displays show it."""
    accounts = "Enabled" if config.applications_enabled else "Disabled"
    return Card(
        "Competition Status",
        [
            Field("Scheduled Start", config.competition_start_time),
            Field("Scheduled End", config.competition_end_time),
            Field("Controlled Applications", ", ".join(config.controlled_applications), wide=True),
            Field("Team Accounts", f"{accounts} ({MAX_TEAMS} teams)"),
        ],
        headline=competition_status(config),
    )


def ticket_update(payload: PostTicketUpdate) -> str | Card:
    """What a ticket's chat says about a change to it."""
    actor = payload.actor
    match payload.action:
        case "resolved":
            return Card(
                "Ticket Resolved",
                [
                    Field("Resolved By", actor),
                    Field("Points Charged", str(payload.points_charged)),
                    Field("Resolution Notes", payload.resolution_notes, wide=True),
                ],
            )
        case "claimed":
            return f"Ticket claimed by **{actor}**"
        case "assigned":
            return f"Ticket assigned to **{payload.assignee}** by **{actor}**"
        case "unclaimed":
            return f"Ticket unclaimed by **{actor}**"
        case "cancelled":
            return f"Ticket cancelled by **{actor}**"
        case "reopened":
            return _with_reason(f"Ticket reopened by **{actor}**", payload.reason)
        case "points_adjusted":
            adjusted = f"Ticket points adjusted to **{payload.points_charged}** by **{actor}**"
            return _with_reason(adjusted, payload.reason)
        case action:
            return f"Ticket updated: {action} by **{actor}**"


def _with_reason(text: str, reason: str) -> str:
    return f"{text}\nReason: {reason}" if reason else text


def ticket_fields(ticket: Ticket) -> list[Field]:
    """A ticket's facts, as its chat shows them. Reads its team, category and assignee."""
    cat = ticket.category
    fields = [
        Field("Team", f"{ticket.team.team_name} (#{ticket.team.team_number})"),
        Field("Status", ticket.status.replace("_", " ").title()),
        Field("Hostname", ticket.hostname),
        Field("Service", ticket.service_name),
        Field("IP Address", ticket.ip_address),
    ]
    if ticket.assigned_to:
        fields.append(Field("Assigned To", ticket.assigned_to.username, wide=True))
    if cat and cat.variable_points:
        fields.append(Field("Point Impact", "Variable"))
    elif cat and cat.points > 0:
        fields.append(Field("Point Impact", f"{cat.points} points"))
    if ticket.resolved_at:
        fields.append(Field("Resolved At", ticket.resolved_at))
        fields.append(Field("Resolution Notes", ticket.resolution_notes, wide=True))
    return fields


def category_name(ticket: Ticket) -> str:
    return ticket.category.display_name if ticket.category else f"Category {ticket.category_id}"
