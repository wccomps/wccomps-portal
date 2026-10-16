"""Staff can assign an open ticket to someone (#38) and change the assignee after close (#36)."""

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse

from core.models import DiscordTask
from team.models import Team
from ticketing.lifecycle import assign_ticket
from ticketing.models import Ticket, TicketCategory, TicketHistory

pytestmark = pytest.mark.django_db


@pytest.fixture
def team():
    return Team.objects.update_or_create(team_number=1, defaults={"team_name": "Team 01"})[0]


@pytest.fixture
def helper(create_user_with_groups):
    return create_user_with_groups("helper", ["WCComps_Ticketing_Support"])


def _ticket(team, status, assignee=None, number="T001-001", points=0):
    return Ticket.objects.create(
        ticket_number=number,
        team=team,
        category=TicketCategory.objects.get(pk=6),
        title="Box is down",
        status=status,
        assigned_to=assignee,
        points_charged=points,
    )


def _assign(actor, ticket, username):
    client = Client()
    client.force_login(actor)
    return client.post(reverse("ticket_reassign", args=[ticket.ticket_number]), {"new_assignee_username": username})


def test_assigning_open_ticket_claims_it_for_that_person(ticketing_support_user, helper, team):
    ticket = _ticket(team, Ticket.STATUS_OPEN)

    _assign(ticketing_support_user, ticket, helper.username)

    ticket.refresh_from_db()
    assert ticket.status == Ticket.STATUS_CLAIMED
    assert ticket.assigned_to == helper
    assert TicketHistory.objects.filter(ticket=ticket, action="claimed").exists()
    assert DiscordTask.objects.filter(task_type="post_ticket_update").exists()


def test_reassigning_claimed_ticket_still_works(ticketing_support_user, helper, team):
    ticket = _ticket(team, Ticket.STATUS_CLAIMED, assignee=ticketing_support_user)

    _assign(ticketing_support_user, ticket, helper.username)

    ticket.refresh_from_db()
    assert (ticket.status, ticket.assigned_to) == (Ticket.STATUS_CLAIMED, helper)


@pytest.mark.parametrize("status", [Ticket.STATUS_RESOLVED, Ticket.STATUS_CANCELLED])
def test_reassigning_closed_ticket_changes_only_the_assignee(ticketing_admin_user, helper, team, status):
    ticket = _ticket(team, status, assignee=ticketing_admin_user, points=60)

    _assign(ticketing_admin_user, ticket, helper.username)

    ticket.refresh_from_db()
    assert ticket.assigned_to == helper
    assert ticket.status == status
    assert ticket.points_charged == 60
    assert TicketHistory.objects.filter(ticket=ticket, action="reassigned").exists()


def test_shared_function_reassigns_closed_and_claims_open(helper, team):
    resolved = _ticket(team, Ticket.STATUS_RESOLVED, number="T001-002")
    opened = _ticket(team, Ticket.STATUS_OPEN, number="T001-003")

    assert assign_ticket(resolved.id, "web:x", user=helper)[0].status == Ticket.STATUS_RESOLVED
    assert assign_ticket(opened.id, "web:x", user=helper)[0].status == Ticket.STATUS_CLAIMED
    assert list(TicketHistory.objects.order_by("ticket_id").values_list("action", flat=True)) == [
        "reassigned",
        "claimed",
    ]


def test_blue_team_cannot_assign(blue_team_user, helper, team):
    ticket = _ticket(team, Ticket.STATUS_OPEN)

    response = _assign(blue_team_user, ticket, helper.username)

    assert response.status_code in (302, 403)
    ticket.refresh_from_db()
    assert ticket.assigned_to is None


def test_assign_box_shown_on_resolved_ticket_with_staff_suggestions(ticketing_support_user, helper, team):
    ticket = _ticket(team, Ticket.STATUS_RESOLVED, assignee=ticketing_support_user)
    client = Client()
    client.force_login(ticketing_support_user)

    html = client.get(reverse("ticket_detail", args=[ticket.ticket_number])).content.decode()

    assert reverse("ticket_reassign", args=[ticket.ticket_number]) in html
    assert f'<option value="{helper.username}"' in html


def test_unknown_user_is_rejected(ticketing_support_user, team):
    ticket = _ticket(team, Ticket.STATUS_OPEN)

    _assign(ticketing_support_user, ticket, "nobody-by-this-name")

    ticket.refresh_from_db()
    assert ticket.status == Ticket.STATUS_OPEN and ticket.assigned_to is None
    assert not User.objects.filter(username="nobody-by-this-name").exists()
