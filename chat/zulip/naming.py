"""Where a ticket lives in Zulip: each team's stream (ZULIP_TEAM_STREAM), and in it one topic per ticket, named by
the ticket's number so a message in the topic maps back to the ticket."""

from django.conf import settings

from team.models import MAX_TEAMS
from ticketing.models import Ticket
from ticketing.utils import TICKET_NUMBER_RE

# Zulip refuses a longer topic name
TOPIC_CHAR_LIMIT = 60


def team_stream(team_number: int) -> str:
    return settings.ZULIP_TEAM_STREAM.format(number=team_number)


def team_for_stream(name: str) -> int | None:
    """The number of the team whose stream this is, or None for any other stream."""
    return next((number for number in range(1, MAX_TEAMS + 1) if team_stream(number) == name), None)


def ticket_topic(ticket: Ticket) -> str:
    """The ticket's topic: its number and title on one line, cut with "…" to Zulip's limit. A topic is plain text,
    so Markdown in the title is shown as typed."""
    topic = f"{ticket.ticket_number}: {' '.join(ticket.title.split())}"
    return topic if len(topic) <= TOPIC_CHAR_LIMIT else topic[: TOPIC_CHAR_LIMIT - 1] + "…"


def ticket_number_from_topic(topic: str) -> str | None:
    """The ticket number a topic starts with, or None."""
    match = TICKET_NUMBER_RE.match(topic)
    return match.group() if match else None
