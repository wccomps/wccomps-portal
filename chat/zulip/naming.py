"""Where a ticket lives in Zulip: each team's stream (ZULIP_TEAM_STREAM), and in it one topic per ticket, named by
the ticket's number so a message in the topic maps back to the ticket. Ticket.zulip_topic owns the name: the first
message sent to the ticket stores it, so a later title edit doesn't move it, and a rename in Zulip (resolving the
topic, moving it) updates it (chat.zulip.incoming), so later messages follow what Zulip shows."""

from functools import cache

from django.conf import settings

from chat.zulip.markup import fit
from team.models import MAX_TEAMS
from ticketing.models import Ticket
from ticketing.utils import TICKET_NUMBER_RE

# Zulip cuts a longer topic name to this, with "..."
TOPIC_CHAR_LIMIT = 60
# What Zulip's "mark as resolved" puts before a topic's name
RESOLVED_TOPIC_PREFIX = "✔ "


def team_stream(team_number: int) -> str:
    return settings.ZULIP_TEAM_STREAM.format(number=team_number)


@cache
def _team_numbers(template: str) -> dict[str, int]:
    return {template.format(number=number): number for number in range(1, MAX_TEAMS + 1)}


def team_for_stream(name: str) -> int | None:
    """The number of the team whose stream this is, or None for any other stream."""
    return _team_numbers(settings.ZULIP_TEAM_STREAM).get(name)


def topic_name(ticket: Ticket) -> str:
    """A new topic's name: the ticket's number and title on one line, cut with "…" to Zulip's limit. A topic is plain
    text, so Markdown in the title is shown as typed."""
    return fit(f"{ticket.ticket_number}: {' '.join(ticket.title.split())}", TOPIC_CHAR_LIMIT)


async def ticket_topic(ticket: Ticket) -> str:
    """The topic a ticket's messages go to: the one stored on it, else a new one (topic_name), stored unless another
    send stored one first, which is then used."""
    if not ticket.zulip_topic:
        tickets = Ticket.objects.filter(pk=ticket.pk)
        if await tickets.filter(zulip_topic="").aupdate(zulip_topic=topic_name(ticket)):
            ticket.zulip_topic = topic_name(ticket)
        else:
            ticket.zulip_topic = await tickets.values_list("zulip_topic", flat=True).aget()
    return ticket.zulip_topic


def ticket_number_from_topic(topic: str) -> str | None:
    """The ticket number a topic starts with, resolved in Zulip or not, or None."""
    match = TICKET_NUMBER_RE.match(topic.removeprefix(RESOLVED_TOPIC_PREFIX))
    return match.group() if match else None
