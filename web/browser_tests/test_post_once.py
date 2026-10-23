"""The post-once guard covers only c-forms: a Django admin action form posts again after an action that only
downloads a file, and a c-form posts once and still sends the button pressed."""

from urllib.parse import parse_qs

import pytest
from django.urls import reverse
from playwright.sync_api import expect

from core.tests.roles import role_user

from .conftest import create_session_context

pytestmark = [pytest.mark.browser, pytest.mark.django_db(transaction=True)]


def test_an_admin_action_after_a_csv_export_reaches_the_server(live_server, pw_browser):

    from team.models import Team
    from ticketing.models import Ticket, TicketCategory

    user = role_user("admin")
    team = Team.objects.create(team_number=4, team_name="Team 04")
    Ticket.objects.create(ticket_number="T004-001", team=team, category=TicketCategory.objects.first(), title="t")
    context = create_session_context(pw_browser, live_server, user)
    page = context.new_page()
    try:
        page.goto(f"{live_server.url}{reverse('admin:ticketing_ticket_changelist')}")
        page.locator("input.action-select").first.check()
        page.select_option("select[name=action]", "export_as_csv")
        with page.expect_download():
            page.get_by_role("button", name="Run").click()
        page.select_option("select[name=action]", "delete_selected")
        with page.expect_navigation():
            page.get_by_role("button", name="Run").click()
        assert "Are you sure" in page.content()
    finally:
        context.close()


def test_a_c_form_posts_once_and_sends_the_button_pressed(live_server, pw_browser):
    from scoring.models import RedTeamIPPool

    user = role_user("red_team")
    RedTeamIPPool.objects.create(name="Pool", ip_addresses="10.0.0.1", created_by=user)
    context = create_session_context(pw_browser, live_server, user)
    page = context.new_page()
    page.on("dialog", lambda dialog: dialog.accept())
    posted = []
    # The post is stopped (as the browser's Stop does): the page stays, its form still marked posting
    page.route("**/ip-pools/*/delete/", lambda route: (posted.append(route.request.post_data), route.abort("aborted")))
    try:
        page.goto(f"{live_server.url}{reverse('scoring:ip_pool_list')}")
        delete = page.get_by_role("button", name="Delete")
        delete.evaluate("button => { button.name = 'choice'; button.value = 'pressed'; }")
        delete.click()
        delete.evaluate("button => button.form.requestSubmit()")
        page.wait_for_timeout(300)
        assert len(posted) == 1
        assert parse_qs(posted[0] or "")["choice"] == ["pressed"]
    finally:
        context.close()


def test_a_submit_button_outside_its_form_is_disabled_while_it_posts(live_server, pw_browser):
    """The bulk approve button sits in the toolbar and joins the table's form with form=."""
    from decimal import Decimal

    from scoring.models import RedTeamScore

    user = role_user("admin")
    RedTeamScore.objects.create(points_per_team=Decimal(10), submitted_by=user)
    context = create_session_context(pw_browser, live_server, user)
    page = context.new_page()
    page.on("dialog", lambda dialog: dialog.accept())
    page.route("**/bulk-approve/", lambda route: route.abort("aborted"))  # stopped: the page stays
    try:
        page.goto(f"{live_server.url}{reverse('scoring:red_team_findings')}")
        page.locator('input[name="finding_ids"]').first.check()
        approve = page.get_by_role("button", name="Approve Selected")
        approve.click()
        expect(approve).to_be_disabled()
    finally:
        context.close()
