"""Teams can't flood Discord by creating (and cancelling) tickets in a loop (security review, Medium)."""

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from core.discord_tasks import TicketCreatedWeb
from core.models import DiscordTask
from team.models import Team
from ticketing.lifecycle import cancel_ticket
from ticketing.models import Ticket, TicketCategory
from ticketing.utils import TEAM_TICKET_LIMIT, TicketRateLimitError, create_ticket_atomic

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _quotient_never_asked():
    """The form reads the synced snapshot; a live Quotient call would hang it while Quotient is down."""
    with patch("quotient.client.get_quotient_client", side_effect=AssertionError("the ticket form asked Quotient")):
        yield


@pytest.fixture
def team(blue_team):
    return blue_team


@pytest.fixture
def category():
    return TicketCategory.objects.get(pk=6)


def test_a_new_ticket_and_its_thread_task_commit_together(team, category):
    """Every ticket, from the web or the bot, gets its Discord thread from the queue."""
    ticket = create_ticket_atomic(team=team, category=category, title="t")

    task = DiscordTask.objects.get(task_type="ticket_created_web")
    assert (task.status, task.typed_payload()) == ("pending", TicketCreatedWeb(ticket_id=ticket.pk))


def test_a_refused_ticket_queues_nothing(team, category):
    _fill(team, category)
    queued = DiscordTask.objects.count()

    with pytest.raises(TicketRateLimitError):
        create_ticket_atomic(team=team, category=category, title="t")

    assert DiscordTask.objects.count() == queued


def _fill(team, category, n=TEAM_TICKET_LIMIT):
    return [create_ticket_atomic(team=team, category=category, title=f"t{i}") for i in range(n)]


def test_limit_blocks_the_next_ticket(team, category):
    _fill(team, category)

    with pytest.raises(TicketRateLimitError):
        create_ticket_atomic(team=team, category=category, title="one too many")
    assert Ticket.objects.filter(team=team).count() == TEAM_TICKET_LIMIT


def test_cancelled_tickets_still_count(team, category):
    """Create-then-cancel is exactly the spam loop; cancelling must not free up a slot."""
    for ticket in _fill(team, category):
        cancel_ticket(ticket.id, actor_username="team01")

    with pytest.raises(TicketRateLimitError):
        create_ticket_atomic(team=team, category=category, title="again")


def test_limit_is_per_team(team, category):
    _fill(team, category)
    other = Team.objects.create(team_number=2, team_name="Team 02")

    assert create_ticket_atomic(team=other, category=category, title="fine").team == other


def test_old_tickets_age_out(team, category):
    tickets = _fill(team, category)
    Ticket.objects.filter(pk__in=[t.pk for t in tickets]).update(created_at=timezone.now() - timedelta(hours=1))

    assert create_ticket_atomic(team=team, category=category, title="later")


def test_staff_can_bypass(team, category):
    _fill(team, category)

    assert create_ticket_atomic(team=team, category=category, title="staff", enforce_team_limit=False)


def _post(user, **extra):
    client = Client()
    client.force_login(user)
    return client.post(reverse("create_ticket"), {"title": "Help", "category": 6, "description": "please", **extra})


def test_web_form_shows_the_limit_to_team_members(blue_team_user, team, category):
    _fill(team, category)

    response = _post(blue_team_user)

    assert response.status_code == 429
    assert b"too many tickets" in response.content
    assert Ticket.objects.filter(team=team).count() == TEAM_TICKET_LIMIT


def test_web_form_lets_ticketing_admins_file_past_the_limit(ticketing_admin_user, team, category):
    _fill(team, category)

    response = _post(ticketing_admin_user, team_id=team.id)

    assert response.status_code == 302
    assert Ticket.objects.filter(team=team).count() == TEAM_TICKET_LIMIT + 1
