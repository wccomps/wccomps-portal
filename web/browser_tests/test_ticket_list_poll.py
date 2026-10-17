"""The ticket list's 30-second refresh swaps only its rows: a new ticket shows up while rows are checked, and the rows
still listed stay checked, with the bulk bar's count."""

import pytest

from core.tests.roles import role_user

from .conftest import create_session_context, make_test_data

pytestmark = [pytest.mark.browser, pytest.mark.django_db(transaction=True)]


def test_the_refresh_lists_a_new_ticket_and_keeps_the_selection(live_server, pw_browser):
    from ticketing.models import Ticket

    ticket = make_test_data()["ticket"]
    context = create_session_context(pw_browser, live_server, role_user("ticketing_support"))
    page = context.new_page()
    try:
        page.clock.install()
        page.goto(f"{live_server.url}/tickets/", wait_until="networkidle")
        page.get_by_label(f"Select ticket {ticket.ticket_number}").check()
        page.get_by_text("1 ticket selected").wait_for()

        Ticket.objects.create(ticket_number="TEAM01-009", title="Arrived later", team=ticket.team, status="open")
        with page.expect_response(lambda r: r.url.startswith(f"{live_server.url}/tickets/")):
            page.clock.run_for("00:31")
        page.get_by_role("gridcell", name="Arrived later").wait_for()

        assert page.get_by_label(f"Select ticket {ticket.ticket_number}").is_checked()
        assert not page.get_by_label("Select ticket TEAM01-009").is_checked()
        assert page.get_by_text("1 ticket selected").is_visible()
        page.get_by_label("Select ticket TEAM01-009").check()
        page.get_by_text("2 tickets selected").wait_for()
    finally:
        context.close()


def test_the_refresh_brings_the_page_links_along(live_server, pw_browser):
    """The refresh swapped only the rows: a ticket that pushed a row onto page 2 left no Next link to reach it."""
    from ticketing.models import Ticket

    team = make_test_data()["team"]
    for n in range(2, 26):
        Ticket.objects.create(ticket_number=f"TEAM01-{n:03d}", title=f"t{n}", team=team, status="open")
    context = create_session_context(pw_browser, live_server, role_user("ticketing_support"))
    page = context.new_page()
    try:
        page.clock.install()
        page.goto(f"{live_server.url}/tickets/?page_size=25", wait_until="networkidle")
        page.get_by_text("Showing 1-25 of 25 results").wait_for()
        assert page.get_by_role("link", name="Next").count() == 0

        Ticket.objects.create(ticket_number="TEAM01-026", title="t26", team=team, status="open")
        with page.expect_response(lambda r: r.url.startswith(f"{live_server.url}/tickets/?")):
            page.clock.run_for("00:31")
        page.get_by_text("Showing 1-25 of 26 results").wait_for()
        assert page.get_by_role("link", name="Next").count() == 1
    finally:
        context.close()
