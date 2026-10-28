"""Ticking several status checkboxes filters the ticket list live (issue #37)."""

import pytest

from core.tests.roles import role_user

from .conftest import create_session_context

pytestmark = [pytest.mark.browser, pytest.mark.django_db(transaction=True)]


def test_ticking_two_statuses_filters_list(live_server, pw_browser):
    from team.models import Team
    from ticketing.models import Ticket, TicketCategory

    team, _ = Team.objects.get_or_create(team_number=1, defaults={"team_name": "Team 01", "is_active": True})
    category = TicketCategory.objects.get(pk=6)
    for i, status in enumerate(["open", "claimed", "resolved", "cancelled"], start=1):
        Ticket.objects.create(
            ticket_number=f"T001-09{i}", team=team, category=category, title=f"{status} ticket", status=status
        )

    user = role_user("ticketing_support")
    context = create_session_context(pw_browser, live_server, user)
    page = context.new_page()
    errors: list[str] = []
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)

    try:
        page.goto(f"{live_server.url}/tickets/")

        def cell(title: str):  # table cells only (a new-ticket toast can repeat the title)
            return page.get_by_role("cell", name=title, exact=True)

        assert cell("resolved ticket").is_visible()

        with page.expect_response(lambda r: "/tickets/" in r.url and "status=" in r.url):
            page.check('input[name="status"][value="open"]')
        with page.expect_response(lambda r: "/tickets/" in r.url and "status=claimed" in r.url):
            page.check('input[name="status"][value="claimed"]')

        cell("resolved ticket").wait_for(state="detached")
        assert cell("open ticket").is_visible()
        assert cell("claimed ticket").is_visible()
        assert cell("cancelled ticket").count() == 0
        assert "status=open" in page.url and "status=claimed" in page.url  # shareable URL
        assert not errors, errors
    finally:
        context.close()
