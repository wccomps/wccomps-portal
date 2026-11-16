import asyncio
from collections.abc import Awaitable, Callable

import pytest
import pytest_asyncio
from asgiref.sync import sync_to_async
from django.contrib.auth.models import User

from chat.zulip.client import ZulipError
from chat.zulip.incoming import IncomingMessage
from core.models import UserGroups
from team.models import Team
from ticketing.models import Ticket, TicketCategory, TicketComment
from ticketing.utils import create_ticket_atomic

MakeTeams = Callable[..., Awaitable[None]]


class FakeZulip:
    """Stands in for ZulipClient: records sends; streams in missing_streams answer STREAM_DOES_NOT_EXIST."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []  # (stream, topic, content)
        self.reactions: list[tuple[int, str]] = []
        self.missing_streams: set[str] = set()
        self.fail_once_on: str | None = None  # a stream whose next send answers HTTP 502
        self.events: list[list[dict[str, object]]] = []  # one list per get_events call
        self.get_events_errors: list[ZulipError] = []
        self.registered = 0
        self.uploads: dict[str, bytes] = {}
        self.fetched: list[str] = []
        self._message_id = 1000

    @property
    def sent_streams(self) -> list[str]:
        return [stream for stream, _, _ in self.sent]

    def sent_to(self, stream: str) -> list[str]:
        return [content for s, _, content in self.sent if s == stream]

    async def send_stream_message(self, stream: str, topic: str, content: str) -> int:
        if stream in self.missing_streams:
            raise ZulipError("STREAM_DOES_NOT_EXIST")
        if stream == self.fail_once_on:
            self.fail_once_on = None
            raise ZulipError("HTTP_502")
        self.sent.append((stream, topic, content))
        self._message_id += 1
        return self._message_id

    async def add_reaction(self, message_id: int, emoji_name: str) -> None:
        self.reactions.append((message_id, emoji_name))

    async def register_queue(self) -> tuple[str, int]:
        self.registered += 1
        return f"queue-{self.registered}", -1

    async def get_events(self, queue_id: str, last_event_id: int) -> list[dict[str, object]]:
        if self.get_events_errors:
            raise self.get_events_errors.pop(0)
        if not self.events:
            raise asyncio.CancelledError  # ends the runner under test
        return self.events.pop(0)

    async def fetch_upload(self, path: str, limit: int) -> bytes:
        self.fetched.append(path)
        return self.uploads[path][:limit]


def message(
    *,
    topic: str,
    stream: str = "team03",
    content: str = "hi",
    message_id: int = 77,
    sender_id: int = 9,
    sender_email: str = "member@example.org",
    sender_name: str = "Member",
) -> IncomingMessage:
    """A message in a Zulip topic, as parse_message_event gives it: by default from team 3's member (the ticket
    fixture's portal account) in team 3's stream."""
    return IncomingMessage(message_id, stream, topic, sender_id, sender_email, sender_name, content)


async def portal_account(username: str, email: str, groups: list[str], *, active: bool = True) -> User:
    """A portal account holding these Authentik groups, as signing in records them."""
    user = await User.objects.acreate(username=username, email=email)
    await UserGroups.objects.acreate(user=user, authentik_id=f"uid-{username}", groups=groups, account_active=active)
    return user


@pytest.fixture
def fake_zulip() -> FakeZulip:
    return FakeZulip()


@pytest_asyncio.fixture
async def make_teams(db: None) -> MakeTeams:
    async def make(active: list[int], total: int = 6) -> None:
        for n in range(1, total + 1):
            await Team.objects.acreate(team_number=n, team_name=f"Team {n}", is_active=n in active)

    return make


@pytest_asyncio.fixture
async def ticket(make_teams: MakeTeams) -> Ticket:
    """Team 3's first ticket, T003-001 (teams 3 and 5 active), with the portal account of team 3's member
    member@example.org. Creating it queues its ticket_created_web rows: a test that counts rows clears them first."""
    await make_teams(active=[3, 5])
    team = await Team.objects.aget(team_number=3)
    await portal_account("member03", "member@example.org", [team.authentik_group])
    category = await TicketCategory.objects.afirst()  # the seeded categories (the root conftest restores them)
    assert category is not None
    return await sync_to_async(create_ticket_atomic)(team, category, "Web server down")


@pytest_asyncio.fixture
async def team5_ticket(ticket: Ticket) -> Ticket:
    team = await Team.objects.aget(team_number=5)
    return await sync_to_async(create_ticket_atomic)(team, ticket.category, "DNS")


@pytest_asyncio.fixture
async def ticket_with_comment(ticket: Ticket) -> tuple[Ticket, TicketComment]:
    return ticket, await TicketComment.objects.acreate(ticket=ticket, comment_text="note")
