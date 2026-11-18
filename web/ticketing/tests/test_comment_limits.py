"""What a team can put into a ticket, from the web as from chat: comments of at most TicketComment.MAX_LENGTH, posts
under one rate limit per ticket and per portal account counted under advisory locks, files only on an open ticket and
within the per-ticket and per-team caps, and a comment or update relayed to chat in at most two messages."""

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import psycopg2
import pytest
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import Client
from django.urls import reverse
from hypothesis import given
from hypothesis import strategies as st

from team.models import Team
from ticketing.models import CommentRateLimit, Ticket, TicketAttachment, TicketCategory, TicketComment
from ticketing.utils import RELAY_MAX_PARTS, relay_parts

URL = "https://portal.example.org/tickets/T001-001/"


@pytest.fixture
def team_ticket(blue_team: Team) -> Ticket:
    return Ticket.objects.create(
        ticket_number="T001-001", team=blue_team, category=TicketCategory.objects.first(), title="Help"
    )


@pytest.fixture
def client(blue_team_user: User) -> Client:
    client = Client()
    client.force_login(blue_team_user)
    return client


def _messages(response: Any) -> list[str]:
    return [str(m) for m in response.context["messages"]]


# ---------------------------------------------------------------------------------------------------------------
# Comment length
# ---------------------------------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_web_comment_past_the_limit_is_refused(client: Client, team_ticket: Ticket) -> None:
    url = reverse("ticket_comment", args=[team_ticket.ticket_number])

    response = client.post(url, {"comment": "x" * (TicketComment.MAX_LENGTH + 1)}, follow=True)

    assert not TicketComment.objects.exists()
    assert not CommentRateLimit.objects.exists()
    assert any(f"at most {TicketComment.MAX_LENGTH} characters" in m for m in _messages(response))


@pytest.mark.django_db
def test_a_web_comment_at_the_limit_is_saved(client: Client, team_ticket: Ticket) -> None:
    url = reverse("ticket_comment", args=[team_ticket.ticket_number])
    client.post(url, {"comment": "x" * TicketComment.MAX_LENGTH})

    assert len(TicketComment.objects.get().comment_text) == TicketComment.MAX_LENGTH


@pytest.mark.django_db
def test_an_empty_web_comment_says_so(client: Client, team_ticket: Ticket) -> None:
    response = client.post(reverse("ticket_comment", args=[team_ticket.ticket_number]), {"comment": ""}, follow=True)

    assert _messages(response) == ["Comment cannot be empty"]


@pytest.mark.django_db
def test_the_comment_box_stops_at_the_limit(client: Client, team_ticket: Ticket) -> None:
    html = client.get(reverse("ticket_detail", args=[team_ticket.ticket_number])).content.decode()

    assert f'maxlength="{TicketComment.MAX_LENGTH}"' in html


# ---------------------------------------------------------------------------------------------------------------
# One rate limit, keyed on the portal account
# ---------------------------------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_web_comments_count_against_the_accounts_chat_budget(
    client: Client, team_ticket: Ticket, blue_team_user: User
) -> None:
    """A team's shared account has one budget: its Discord and Zulip posts count against its web ones."""
    for _ in range(CommentRateLimit.ACTOR_LIMIT):
        CommentRateLimit.objects.create(ticket=None, actor_key=CommentRateLimit.user_key(blue_team_user))

    response = client.post(reverse("ticket_comment", args=[team_ticket.ticket_number]), {"comment": "hi"}, follow=True)

    assert not TicketComment.objects.exists()
    assert any("User rate limit exceeded" in m for m in _messages(response))


@pytest.mark.django_db
def test_web_uploads_count_against_the_rate_limit(client: Client, team_ticket: Ticket, blue_team_user: User) -> None:
    for _ in range(CommentRateLimit.ACTOR_LIMIT):
        CommentRateLimit.objects.create(ticket=None, actor_key=CommentRateLimit.user_key(blue_team_user))

    response = _upload(client, team_ticket)

    assert not TicketAttachment.objects.exists()
    assert any("User rate limit exceeded" in m for m in _messages(response))


@pytest.mark.django_db
def test_web_uploads_are_counted(client: Client, team_ticket: Ticket, blue_team_user: User) -> None:
    _upload(client, team_ticket)

    assert list(CommentRateLimit.objects.values_list("actor_key", flat=True)) == [
        CommentRateLimit.user_key(blue_team_user)
    ]


@contextmanager
def _held_elsewhere(key: str) -> Iterator[None]:
    """The advisory lock on key, held by another session."""
    db = connection.settings_dict
    other = psycopg2.connect(
        host=db["HOST"], port=db["PORT"], user=db["USER"], password=db["PASSWORD"], dbname=db["NAME"]
    )
    other.autocommit = True
    other.cursor().execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", [key])
    try:
        yield
    finally:
        other.cursor().execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", [key])
        other.close()


def _waits_for(key: str, step: Callable[[], object]) -> None:
    """step blocks while another session holds key's lock, and finishes once it is let go."""

    def run() -> None:
        try:
            step()
        finally:
            connection.close()

    worker = threading.Thread(target=run)
    with _held_elsewhere(key):
        worker.start()
        worker.join(timeout=2)
        assert worker.is_alive(), f"ran without waiting for the lock on {key}"
    worker.join(timeout=30)
    assert not worker.is_alive()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("lock", ["ticket", "actor"])
def test_the_rate_limit_counts_under_the_ticket_and_actor_locks(team_ticket: Ticket, lock: str) -> None:
    key = f"comment_rate:ticket:{team_ticket.pk}" if lock == "ticket" else "comment_rate:actor:user:1"

    _waits_for(key, lambda: CommentRateLimit.take(team_ticket, "user:1"))

    assert CommentRateLimit.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_posts_at_once_dont_pass_the_tickets_limit(team_ticket: Ticket) -> None:
    posts = CommentRateLimit.TICKET_LIMIT * 2
    start = threading.Barrier(posts)
    refused: list[str] = []

    def post(n: int) -> None:
        try:
            start.wait()
            if reason := CommentRateLimit.take(team_ticket, f"user:{n}"):
                refused.append(reason)
        finally:
            connection.close()

    workers = [threading.Thread(target=post, args=(n,)) for n in range(posts)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=30)

    assert CommentRateLimit.objects.count() == CommentRateLimit.TICKET_LIMIT
    assert len(refused) == posts - CommentRateLimit.TICKET_LIMIT


# ---------------------------------------------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------------------------------------------


def _upload(client: Client, ticket: Ticket, data: bytes = b"log") -> Any:
    upload = SimpleUploadedFile("a.txt", data, content_type="text/plain")
    url = reverse("ticket_attachment_upload", args=[ticket.ticket_number])
    return client.post(url, {"attachment": upload}, follow=True)


def _attach(ticket: Ticket, data: bytes = b"x") -> None:
    TicketAttachment.objects.create(
        ticket=ticket, file_data=data, filename="old.txt", mime_type="text/plain", uploaded_by="x"
    )


@pytest.mark.django_db
@pytest.mark.parametrize("status", [Ticket.STATUS_RESOLVED, Ticket.STATUS_CANCELLED])
def test_a_closed_ticket_takes_no_web_uploads(client: Client, team_ticket: Ticket, status: str) -> None:
    Ticket.objects.filter(pk=team_ticket.pk).update(status=status)

    response = _upload(client, team_ticket)

    assert not TicketAttachment.objects.exists()
    assert f"Ticket is {status}; files can't be added to it" in _messages(response)


@pytest.mark.django_db
def test_a_ticket_takes_at_most_its_cap_of_files(client: Client, team_ticket: Ticket) -> None:
    for _ in range(TicketAttachment.MAX_PER_TICKET):
        _attach(team_ticket)

    response = _upload(client, team_ticket)

    assert TicketAttachment.objects.count() == TicketAttachment.MAX_PER_TICKET
    assert f"Ticket already has the maximum of {TicketAttachment.MAX_PER_TICKET} attachments" in _messages(response)


@pytest.mark.django_db
def test_a_teams_tickets_together_hold_at_most_its_byte_cap(
    client: Client, team_ticket: Ticket, blue_team: Team, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Counted over all the team's tickets, so opening another doesn't reset it; another team's files don't count."""
    monkeypatch.setattr(TicketAttachment, "MAX_TEAM_BYTES", 10)
    other_ticket = Ticket.objects.create(
        ticket_number="T001-002", team=blue_team, category=TicketCategory.objects.first(), title="More"
    )
    _attach(other_ticket, b"x" * 8)
    other_team = Team.objects.create(team_number=2, team_name="Team 02")
    _attach(Ticket.objects.create(ticket_number="T002-001", team=other_team, title="Theirs"), b"x" * 100)

    assert _upload(client, team_ticket, b"ab").status_code == 200
    refused = _upload(client, team_ticket, b"c")

    assert TicketAttachment.objects.filter(ticket=team_ticket).count() == 1
    assert any("total limit" in m for m in _messages(refused))


@pytest.mark.django_db(transaction=True)
def test_a_file_is_checked_and_saved_under_its_teams_lock(team_ticket: Ticket) -> None:
    attachment = TicketAttachment.with_file(b"log", "a.txt", ticket=team_ticket, uploaded_by="x")

    _waits_for(f"ticket_attachments:team:{team_ticket.team_id}", attachment.store)

    assert TicketAttachment.objects.count() == 1


# ---------------------------------------------------------------------------------------------------------------
# Relaying to chat
# ---------------------------------------------------------------------------------------------------------------


def test_text_that_fits_is_relayed_whole() -> None:
    assert relay_parts("short", 20, URL) == ["short"]


def test_long_text_is_cut_to_two_parts_ending_with_the_ticket_link() -> None:
    parts = relay_parts("\n".join("y" * 99 for _ in range(45)), 2000, URL)

    assert len(parts) == RELAY_MAX_PARTS
    assert all(len(part) <= 2000 for part in parts)
    assert parts[-1].endswith(f"The rest is on the portal: {URL}")


@given(text=st.text(alphabet=st.characters(codec="utf-8") | st.just("\n"), max_size=600))
def test_any_text_is_relayed_in_at_most_two_parts_under_the_limit(text: str) -> None:
    parts = relay_parts(text, 120, URL)

    assert len(parts) <= RELAY_MAX_PARTS
    assert all(len(part) <= 120 for part in parts)
    # Nothing is lost unless the last part points to the rest
    if not parts[-1].endswith(URL):
        assert "".join(parts).replace("\n", "") == text.replace("\n", "")
