"""A ticket page's way back returns to the list as its own tab last showed it, whatever another tab does."""

from urllib.parse import parse_qs, urlsplit

import pytest
from django.urls import reverse

from .conftest import _create_role_user, create_session_context

pytestmark = [pytest.mark.browser, pytest.mark.django_db(transaction=True)]


def back_link_query(page) -> dict[str, list[str]]:
    return parse_qs(urlsplit(page.locator(".breadcrumbs a").first.get_attribute("href") or "").query)


def test_each_tab_returns_to_its_own_list(live_server, pw_browser):
    from team.models import Team
    from ticketing.models import Ticket, TicketCategory

    user = _create_role_user("admin", None)
    team = Team.objects.create(team_number=4, team_name="Team 04")
    ticket = Ticket.objects.create(
        ticket_number="T004-001", team=team, category=TicketCategory.objects.first(), title="t"
    )
    detail = f"{live_server.url}{reverse('ticket_detail', kwargs={'ticket_number': ticket.ticket_number})}"
    context = create_session_context(pw_browser, live_server, user)
    tab_a, tab_b = context.new_page(), context.new_page()
    try:
        tab_a.goto(f"{live_server.url}{reverse('ticket_list')}?search=t&page_size=25")
        tab_b.goto(f"{live_server.url}{reverse('ticket_list')}")
        tab_b.fill("#id_search", "other")  # a real filter change in tab B, through the toolbar
        with tab_b.expect_response(lambda r: "search=other" in r.url):
            tab_b.locator("#id_search").press("Enter")
        tab_b.wait_for_load_state("networkidle")
        tab_a.goto(detail)
        tab_b.goto(detail)
        assert back_link_query(tab_a) == {"search": ["t"], "page_size": ["25"]}
        assert back_link_query(tab_b)["search"] == ["other"]
    finally:
        context.close()


def test_without_a_list_shown_the_way_back_is_the_plain_list(live_server, pw_browser):
    from team.models import Team
    from ticketing.models import Ticket, TicketCategory

    user = _create_role_user("admin", None)
    team = Team.objects.create(team_number=4, team_name="Team 04")
    ticket = Ticket.objects.create(
        ticket_number="T004-001", team=team, category=TicketCategory.objects.first(), title="t"
    )
    context = create_session_context(pw_browser, live_server, user)
    page = context.new_page()
    try:
        page.goto(f"{live_server.url}{reverse('ticket_detail', kwargs={'ticket_number': ticket.ticket_number})}")
        assert page.locator(".breadcrumbs a").first.get_attribute("href") == reverse("ticket_list")
    finally:
        context.close()


def test_after_going_back_between_filters_the_way_back_is_the_list_shown(live_server, pw_browser):
    from team.models import Team
    from ticketing.models import Ticket, TicketCategory

    user = _create_role_user("admin", None)
    ticket = Ticket.objects.create(
        ticket_number="T004-001",
        team=Team.objects.create(team_number=4, team_name="Team 04"),
        category=TicketCategory.objects.first(),
        title="t",
    )
    context = create_session_context(pw_browser, live_server, user)
    page = context.new_page()
    try:
        page.goto(f"{live_server.url}{reverse('ticket_list')}?search=first")
        page.fill("#id_search", "second")
        with page.expect_response(lambda r: "search=second" in r.url):
            page.locator("#id_search").press("Enter")
        page.wait_for_load_state("networkidle")
        page.go_back()  # htmx restores the first filter's list
        page.wait_for_function("location.search.includes('search=first')")
        page.goto(f"{live_server.url}{reverse('ticket_detail', kwargs={'ticket_number': ticket.ticket_number})}")
        assert back_link_query(page)["search"] == ["first"]
    finally:
        context.close()
