"""Live comment/history polling on the ticket detail page."""

import re

import pytest
from django.test import Client
from django.urls import reverse

from team.models import Team
from ticketing.models import Ticket, TicketCategory

pytestmark = pytest.mark.django_db


@pytest.fixture
def tickets():
    own = Team.objects.update_or_create(
        team_number=1, defaults={"team_name": "Team 01", "authentik_group": "WCComps_BlueTeam01"}
    )[0]
    other = Team.objects.create(team_number=2, team_name="Team 02", authentik_group="WCComps_BlueTeam02")
    category = TicketCategory.objects.get(pk=6)
    return (
        Ticket.objects.create(team=own, ticket_number="T001-001", category=category, title="Own"),
        Ticket.objects.create(team=other, ticket_number="T002-001", category=category, title="Other"),
    )


def _client(user):
    client = Client()
    client.force_login(user)
    return client


def test_team_member_polls_own_ticket(blue_team_user, tickets):
    response = _client(blue_team_user).get(reverse("ticket_detail_dynamic", args=["T001-001"]))
    assert response.status_code == 200
    assert b'id="comments-list"' in response.content


def test_team_member_cannot_poll_other_teams_ticket(blue_team_user, tickets):
    response = _client(blue_team_user).get(reverse("ticket_detail_dynamic", args=["T002-001"]))
    assert response.status_code == 403


@pytest.mark.parametrize("list_id", ["comments-list", "history-list"])
def test_polled_list_is_not_the_polling_element(ticketing_support_user, tickets, list_id):
    """The partial's list has no hx-* attributes, so swapping it over the poller would stop polling."""
    html = _client(ticketing_support_user).get(reverse("ticket_detail", args=["T001-001"])).content.decode()
    tag = re.search(rf'<div[^>]*id="{list_id}"[^>]*>', html)
    assert tag and "hx-get" not in tag.group(0)
    assert f'hx-select="#{list_id}"' in html


def _with_staff_history(ticket):
    from django.contrib.auth.models import User

    from ticketing.models import TicketHistory

    staff = User.objects.create(username="staff-volunteer")
    TicketHistory.objects.create(
        ticket=ticket, action="verified", actor=staff, details={"approval_notes": "staff-only note"}
    )


def test_team_poll_carries_no_history(blue_team_user, tickets):
    """History holds staff notes, points and usernames; the team page never shows it, so the poll mustn't send it."""
    _with_staff_history(tickets[0])

    content = _client(blue_team_user).get(reverse("ticket_detail_dynamic", args=["T001-001"])).content

    assert b"staff-volunteer" not in content
    assert b"staff-only note" not in content


def test_staff_poll_carries_history(ticketing_support_user, tickets):
    _with_staff_history(tickets[0])

    content = _client(ticketing_support_user).get(reverse("ticket_detail_dynamic", args=["T001-001"])).content

    assert b"staff-volunteer" in content


# Each list carries a version of what it shows; the poll sends it back and gets 204 (htmx swaps nothing) unless
# something changed, so the aria-live wrapper only announces real changes.
def _version(html: str, name: str) -> str:
    match = re.search(rf'name="{name}"\s+value="([^"]*)"', html)
    assert match, f"no {name} in the page"
    return match.group(1)


@pytest.mark.parametrize("name", ["comments_version", "history_version"])
def test_an_unchanged_poll_answers_204(ticketing_support_user, tickets, name):
    client = _client(ticketing_support_user)
    version = _version(client.get(reverse("ticket_detail", args=["T001-001"])).content.decode(), name)

    response = client.get(reverse("ticket_detail_dynamic", args=["T001-001"]), {name: version})

    assert response.status_code == 204
    assert response.content == b""


def test_a_new_comment_makes_the_next_poll_answer_with_it(ticketing_support_user, tickets):
    from ticketing.models import TicketComment

    client = _client(ticketing_support_user)
    version = _version(client.get(reverse("ticket_detail", args=["T001-001"])).content.decode(), "comments_version")
    TicketComment.objects.create(ticket=tickets[0], author=ticketing_support_user, comment_text="Box is back up")

    response = client.get(reverse("ticket_detail_dynamic", args=["T001-001"]), {"comments_version": version})

    assert response.status_code == 200
    html = response.content.decode()
    assert "Box is back up" in html
    assert _version(html, "comments_version") != version


def test_new_history_makes_the_next_history_poll_answer(ticketing_support_user, tickets):
    client = _client(ticketing_support_user)
    version = _version(client.get(reverse("ticket_detail", args=["T001-001"])).content.decode(), "history_version")
    _with_staff_history(tickets[0])

    response = client.get(reverse("ticket_detail_dynamic", args=["T001-001"]), {"history_version": version})

    assert response.status_code == 200
    assert "verified" in response.content.decode()
