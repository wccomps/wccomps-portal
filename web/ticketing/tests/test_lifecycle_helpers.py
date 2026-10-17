"""Lifecycle helpers shared by the web, the bot and the Discord dashboard."""

import pytest
from django.test import Client
from django.urls import reverse

from core.models import DiscordTask
from ticketing.lifecycle import cancel_ticket, reopen_ticket
from ticketing.models import Ticket, TicketCategory, TicketHistory
from ticketing.utils import change_ticket_category_atomic

pytestmark = pytest.mark.django_db


@pytest.fixture
def team(blue_team):
    return blue_team


def _ticket(team, status, **extra):
    return Ticket.objects.create(
        ticket_number=f"T001-{Ticket.objects.count() + 1:03d}",
        team=team,
        category=TicketCategory.objects.get(pk=2),
        title="t",
        status=status,
        **extra,
    )


def test_team_cannot_cancel_claimed_but_staff_can(team):
    ticket = _ticket(team, "claimed")

    _, error = cancel_ticket(ticket.id, "team01")
    assert error == "Claimed tickets can only be cancelled by ticketing staff."

    cancelled, error = cancel_ticket(ticket.id, "admin", reason="duplicate", staff=True)
    assert error is None
    assert (cancelled.status, cancelled.resolution_notes, cancelled.points_charged) == ("cancelled", "duplicate", 0)


def test_cancel_schedules_thread_archive(team):
    ticket = _ticket(team, "open", discord_thread_id=555)
    cancelled, _ = cancel_ticket(ticket.id, "team01")
    assert cancelled.thread_archive_scheduled_at is not None


def test_resolved_or_cancelled_ticket_cannot_be_cancelled(team):
    for status in ("resolved", "cancelled"):
        _, error = cancel_ticket(_ticket(team, status).id, "admin", staff=True)
        assert error == f"Cannot cancel ticket with status: {status}."


def test_reopen_refunds_points(team):
    ticket = _ticket(team, "resolved", points_charged=60)
    reopened, _ = reopen_ticket(ticket.id, "admin", reopen_reason="not fixed")
    assert (reopened.status, reopened.points_charged) == ("open", 0)
    assert TicketHistory.objects.get(ticket=ticket, action="reopened").details["refunded_points"] == 60


def test_change_category_records_both_categories(team, ticketing_admin_user):
    ticket = _ticket(team, "open")
    changed, error = change_ticket_category_atomic(ticket.id, 3, "admin", user=ticketing_admin_user)
    assert error is None and changed.category_id == 3
    details = TicketHistory.objects.get(ticket=ticket, action="category_changed").details
    assert (details["old_category"], details["new_category"], details["changed_by"]) == (2, 3, "admin")

    assert (
        change_ticket_category_atomic(ticket.id, 3, "admin", user=ticketing_admin_user)[1]
        == f"Ticket {ticket.ticket_number} is already in that category."
    )
    assert change_ticket_category_atomic(ticket.id, 999, "admin", user=ticketing_admin_user)[1] == "Invalid category."


def test_web_cancel_tells_discord(blue_team_user, team):
    ticket = _ticket(team, "open")
    client = Client()
    client.force_login(blue_team_user)
    client.post(reverse("ticket_cancel", args=[ticket.ticket_number]))

    task = DiscordTask.objects.get(task_type="post_ticket_update", ticket=ticket)
    assert task.payload["action"] == "cancelled"
