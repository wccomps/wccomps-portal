"""Every bulk approve is an ordinary form submit: it asks a question that counts the checked rows (the confirm
listener in static/js/utils.js), and the form's guard lets one POST through however long the server takes."""

import pytest
from django.urls import reverse

from .conftest import _create_role_user, create_session_context
from .table_data import seed_table_data

pytestmark = [pytest.mark.browser, pytest.mark.django_db(transaction=True)]


def unapproved(kind: str) -> int:
    from scoring.models import IncidentReport, InjectScore, OrangeTeamScore, RedTeamScore

    from ticketing.models import Ticket

    return {
        "incident": lambda: IncidentReport.objects.filter(is_approved=False).count(),
        "check": lambda: OrangeTeamScore.objects.filter(is_approved=False).count(),
        "finding": lambda: RedTeamScore.objects.filter(is_approved=False).count(),
        "grade": lambda: InjectScore.objects.filter(is_approved=False).count(),
        "ticket": lambda: Ticket.objects.filter(status="resolved", is_approved=False).count(),
        "feedback item": lambda: InjectScore.objects.filter(is_approved=True, feedback_approved=False).count(),
    }[kind]()


PAGES = [
    ("scoring:review_incidents", "incident"),
    ("scoring:review_orange", "check"),
    ("scoring:red_team_findings", "finding"),
    ("scoring:inject_grades_review", "grade"),
    ("ops_review_tickets", "ticket"),
    ("scoring:review_inject_feedback", "feedback item"),
]


@pytest.mark.parametrize(("name", "kind"), PAGES)
def test_a_bulk_approve_asks_with_the_count_and_a_dismissal_approves_nothing(live_server, pw_browser, name, kind):
    user = _create_role_user("admin", None)
    seed_table_data(user)
    before = unapproved(kind)
    context = create_session_context(pw_browser, live_server, user)
    page = context.new_page()
    questions: list[str] = []
    answer = {"accept": False}

    def reply(dialog):
        questions.append(dialog.message)
        dialog.accept() if answer["accept"] else dialog.dismiss()

    page.on("dialog", reply)
    try:
        page.goto(f"{live_server.url}{reverse(name)}")
        page.locator('tbody input[type="checkbox"]').first.check()
        page.get_by_role("button", name="Approve Selected").click()
        page.wait_for_load_state("networkidle")
        assert questions == [f"Approve 1 {kind}(s)? This cannot be undone."]
        assert unapproved(kind) == before
        answer["accept"] = True
        with page.expect_navigation():
            page.get_by_role("button", name="Approve Selected").click()
        assert unapproved(kind) == before - 1
    finally:
        context.close()


def test_a_form_whose_post_is_slow_posts_once(live_server, pw_browser):
    """The guard holds until the page goes away, not for a fixed time: a second submit 15 s later is still blocked.
    A window listener counts the submits that get through and stops them leaving the page."""
    from scoring.models import RedTeamIPPool

    user = _create_role_user("red_team", None)
    RedTeamIPPool.objects.create(name="Pool", ip_addresses="10.0.0.1", created_by=user)
    context = create_session_context(pw_browser, live_server, user)
    page = context.new_page()
    page.clock.install()
    page.on("dialog", lambda dialog: dialog.accept())
    try:
        page.goto(f"{live_server.url}{reverse('scoring:ip_pool_list')}")
        page.evaluate(
            """() => {
                window.posted = 0;
                window.addEventListener('submit', (e) => {
                    if (!e.defaultPrevented) window.posted++;
                    e.preventDefault();
                });
            }"""
        )
        delete = page.get_by_role("button", name="Delete")
        delete.click()
        page.clock.fast_forward(15_000)
        # The guard also disables the button, so submit the form directly
        delete.evaluate("button => button.form.requestSubmit()")
        assert page.evaluate("window.posted") == 1
    finally:
        context.close()


def test_the_ticket_lists_refresh_waits_while_a_bulk_claim_is_posting(live_server, pw_browser):
    """A refresh would replace the list's forms with fresh ones mid-POST, so it doesn't run while one is posting."""
    from team.models import Team
    from ticketing.models import Ticket, TicketCategory

    user = _create_role_user("admin", None)
    team = Team.objects.create(team_number=4, team_name="Team 04")
    Ticket.objects.create(ticket_number="T004-001", team=team, category=TicketCategory.objects.first(), title="t")
    context = create_session_context(pw_browser, live_server, user)
    page = context.new_page()
    page.clock.install()
    page.on("dialog", lambda dialog: dialog.accept())
    try:
        page.goto(f"{live_server.url}{reverse('ticket_list')}")
        page.evaluate(
            """() => {
                window.posted = 0;
                window.addEventListener('submit', (e) => {
                    if (!e.defaultPrevented) window.posted++;
                    e.preventDefault();
                });
            }"""
        )
        page.locator('tbody input[type="checkbox"]').first.check()
        page.get_by_role("button", name="Claim Selected").click()
        page.clock.fast_forward(31_000)  # the refresh's turn
        page.wait_for_load_state("networkidle")
        # The list's own Claim form, whichever one the page holds now (it may be hidden with nothing selected)
        page.evaluate("[...document.forms].find((form) => form.action.endsWith('/bulk-claim/')).requestSubmit()")
        assert page.evaluate("window.posted") == 1
    finally:
        context.close()
