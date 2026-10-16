"""The ticket list's column titles line up, and sorting by a column shows it and can be reversed."""

import pytest

from core.tests.roles import role_user

from .conftest import create_session_context

pytestmark = [pytest.mark.browser, pytest.mark.django_db(transaction=True)]


def test_headers_align_and_show_the_current_sort(live_server, pw_browser):
    from team.models import Team
    from ticketing.models import Ticket, TicketCategory

    team, _ = Team.objects.get_or_create(team_number=1, defaults={"team_name": "Team 01", "is_active": True})
    Ticket.objects.create(
        ticket_number="T001-091", team=team, category=TicketCategory.objects.get(pk=6), title="t", status="open"
    )
    context = create_session_context(pw_browser, live_server, role_user("ticketing_support"))
    page = context.new_page()

    try:
        page.goto(f"{live_server.url}/tickets/?sort=team__team_number")

        # Where each title's text starts: sortable ones sit in a padded link, the rest must match
        text_tops = page.evaluate(
            """() => [...document.querySelectorAll('#result_list thead th .text')]
                .map(t => Math.round((t.querySelector('a, span') || t).getBoundingClientRect().top
                    + parseFloat(getComputedStyle(t.querySelector('a, span') || t).paddingTop)))"""
        )
        assert len(text_tops) == 8 and len(set(text_tops)) == 1, text_tops

        team_header = page.locator("#result_list thead th", has_text="Team")
        assert "▲" in team_header.inner_text()
        assert "sort=-team__team_number" in (team_header.locator("a").get_attribute("href") or "")
    finally:
        context.close()
