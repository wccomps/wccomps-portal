"""Every web caller changes a ticket through ticketing.lifecycle: one history entry, one thread update."""

import pytest
from django.test import Client
from django.urls import reverse

from core.models import DiscordTask
from team.models import DiscordLink, Team
from ticketing import lifecycle
from ticketing.models import Ticket, TicketCategory, TicketHistory

pytestmark = pytest.mark.django_db

THREAD_ID = 4242


@pytest.fixture
def team():
    return Team.objects.create(team_number=1, team_name="Team 01", authentik_group="WCComps_BlueTeam01")


def _ticket(team, status, number="T001-001", **extra):
    return Ticket.objects.create(
        ticket_number=number,
        team=team,
        category=TicketCategory.objects.get(pk=2),
        title="t",
        status=status,
        discord_thread_id=THREAD_ID + int(number[-3:]),
        **extra,
    )


def _effects(ticket):
    """(history actions, post_ticket_update actions, discord ids invited to the thread)."""
    tasks = DiscordTask.objects.filter(ticket=ticket).order_by("pk")
    return (
        list(TicketHistory.objects.filter(ticket=ticket).order_by("pk").values_list("action", flat=True)),
        [t.payload["action"] for t in tasks if t.task_type == "post_ticket_update"],
        [t.payload["discord_id"] for t in tasks if t.task_type == "add_user_to_thread"],
    )


def _post(user, name, ticket, data=None):
    client = Client()
    client.force_login(user)
    return client.post(reverse(name, args=[ticket.ticket_number]), data or {})


def _discord_id(user):
    return DiscordLink.objects.get(user=user).discord_id


@pytest.mark.parametrize(
    ("action", "call"),
    [
        ("claim", lambda t, u: lifecycle.claim_ticket(t.id, "x", user=u)),
        ("unclaim", lambda t, u: lifecycle.unclaim_ticket(t.id, "x", user=u)),
        ("resolve", lambda t, u: lifecycle.resolve_ticket(t.id, "x", user=u)),
        ("cancel", lambda t, u: lifecycle.cancel_ticket(t.id, "x", user=u, staff=True)),
        ("reopen", lambda t, u: lifecycle.reopen_ticket(t.id, "x", user=u)),
    ],
)
@pytest.mark.parametrize("status", ["open", "claimed", "resolved", "cancelled"])
def test_transitions_outside_the_table_are_refused_with_no_effects(ticketing_admin_user, team, action, call, status):
    ticket = _ticket(team, status, assigned_to=ticketing_admin_user if status == "claimed" else None)

    changed, error = call(ticket, ticketing_admin_user)

    allowed = status in lifecycle.TRANSITIONS[action][0]
    assert (changed is not None, error is None) == (allowed, allowed)
    if not allowed:
        assert error == lifecycle.TRANSITIONS[action][1].format(status=status)
        assert _effects(ticket) == ([], [], [])
        ticket.refresh_from_db()
        assert ticket.status == status


def test_claim_view(ticketing_support_user, team):
    ticket = _ticket(team, "open")
    _post(ticketing_support_user, "ticket_claim", ticket)
    assert _effects(ticket) == (["claimed"], ["claimed"], [_discord_id(ticketing_support_user)])


def test_unclaim_view(ticketing_support_user, team):
    ticket = _ticket(team, "claimed", assigned_to=ticketing_support_user)
    _post(ticketing_support_user, "ticket_unclaim", ticket)
    assert _effects(ticket) == (["unclaimed"], ["unclaimed"], [])


@pytest.mark.parametrize(
    ("status", "history"), [("open", "claimed"), ("claimed", "reassigned"), ("resolved", "reassigned")]
)
def test_assign_view(ticketing_admin_user, create_user_with_groups, team, status, history):
    helper = create_user_with_groups("helper", ["WCComps_Ticketing_Support"])
    ticket = _ticket(team, status)
    _post(ticketing_admin_user, "ticket_reassign", ticket, {"new_assignee_username": helper.username})

    assert _effects(ticket) == ([history], ["assigned"], [_discord_id(helper)])
    assert DiscordTask.objects.get(task_type="post_ticket_update").payload["assignee"] == helper.username


def test_resolve_view(ticketing_support_user, team):
    ticket = _ticket(team, "claimed", assigned_to=ticketing_support_user)
    _post(ticketing_support_user, "ticket_resolve", ticket, {"resolution_notes": "done"})
    assert _effects(ticket) == (["resolved"], ["resolved"], [])


def test_reopen_view(ticketing_admin_user, team):
    ticket = _ticket(team, "resolved")
    _post(ticketing_admin_user, "ticket_reopen", ticket, {"reopen_reason": "not fixed"})
    assert _effects(ticket) == (["reopened"], ["reopened"], [])


def test_reopen_refused_once_approved(ticketing_admin_user, team):
    ticket = _ticket(team, "resolved", is_approved=True, points_charged=25)

    changed, error = lifecycle.reopen_ticket(ticket.id, "x", user=ticketing_admin_user)

    assert changed is None
    assert error == "Cannot reopen an approved ticket."
    assert _effects(ticket) == ([], [], [])
    ticket.refresh_from_db()
    assert ticket.status == "resolved"
    assert ticket.is_approved
    assert ticket.points_charged == 25


def test_cancel_view(blue_team_user, team):
    ticket = _ticket(team, "open")
    _post(blue_team_user, "ticket_cancel", ticket)
    assert _effects(ticket) == (["cancelled"], ["cancelled"], [])


def test_bulk_claim_matches_single_claim(ticketing_support_user, team):
    tickets = [_ticket(team, "open", number=f"T001-00{n}") for n in (1, 2)]
    client = Client()
    client.force_login(ticketing_support_user)
    client.post(reverse("tickets_bulk_claim"), {"ticket_numbers": ",".join(t.ticket_number for t in tickets)})

    for ticket in tickets:
        assert _effects(ticket) == (["claimed"], ["claimed"], [_discord_id(ticketing_support_user)])


def test_bulk_resolve_matches_single_resolve(ticketing_admin_user, team):
    tickets = [_ticket(team, "claimed", number=f"T001-00{n}") for n in (1, 2)]
    client = Client()
    client.force_login(ticketing_admin_user)
    client.post(reverse("tickets_bulk_resolve"), {"ticket_numbers": ",".join(t.ticket_number for t in tickets)})

    for ticket in tickets:
        assert _effects(ticket) == (["resolved"], ["resolved"], [])
