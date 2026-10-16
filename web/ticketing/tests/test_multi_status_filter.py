"""The ticket list can filter by several statuses at once (issue #37)."""

import pytest
from django.test import Client
from django.urls import reverse

from ticketing.models import Ticket, TicketCategory

pytestmark = pytest.mark.django_db


@pytest.fixture
def tickets(blue_team):
    team = blue_team
    category = TicketCategory.objects.get(pk=6)
    return {
        status: Ticket.objects.create(
            ticket_number=f"T001-00{i}", team=team, category=category, title=f"{status} ticket", status=status
        )
        for i, status in enumerate(["open", "claimed", "resolved", "cancelled"], start=1)
    }


def _listed(user, query: str) -> set[str]:
    client = Client()
    client.force_login(user)
    html = client.get(reverse("ticket_list") + query).content.decode()
    return {status for status in ("open", "claimed", "resolved", "cancelled") if f"{status} ticket" in html}


def test_checkbox_style_multiple_params(ticketing_support_user, tickets):
    assert _listed(ticketing_support_user, "?status=open&status=claimed") == {"open", "claimed"}


def test_comma_style_used_by_sort_and_page_links(ticketing_support_user, tickets):
    assert _listed(ticketing_support_user, "?status=open,claimed") == {"open", "claimed"}


@pytest.mark.parametrize("query", ["", "?status=all", "?status=", "?status=bogus"])
def test_no_valid_status_means_all(ticketing_support_user, tickets, query):
    assert _listed(ticketing_support_user, query) == {"open", "claimed", "resolved", "cancelled"}


def test_single_status_links_still_work(ticketing_support_user, tickets):
    assert _listed(ticketing_support_user, "?status=resolved") == {"resolved"}


def test_selection_survives_in_links_and_checkboxes(ticketing_support_user, tickets):
    client = Client()
    client.force_login(ticketing_support_user)

    html = client.get(reverse("ticket_list") + "?status=claimed&status=open").content.decode()

    assert "status=open,claimed" in html  # sort/pagination/detail links keep both
    for status, checked in (("open", True), ("claimed", True), ("resolved", False)):
        box = html.split(f'value="{status}"')[1].split(">")[0]
        assert ("checked" in box) is checked


def test_team_view_filters_too(blue_team_user, tickets):
    assert _listed(blue_team_user, "?status=resolved&status=cancelled") == {"resolved", "cancelled"}
