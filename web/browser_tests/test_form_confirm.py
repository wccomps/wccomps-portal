"""A form's data-confirm question is asked before it submits (admin/base_site.html): a dismissed question keeps the
form from submitting, and an empty one (a confirm that applies only sometimes) asks nothing."""

import pytest
from django.urls import reverse

from .conftest import _create_role_user, create_session_context

pytestmark = [pytest.mark.browser, pytest.mark.django_db(transaction=True)]


def open_dashboard(live_server, pw_browser, user, answer: bool):
    context = create_session_context(pw_browser, live_server, user)
    page = context.new_page()
    questions: list[str] = []

    def reply(dialog):
        questions.append(dialog.message)
        dialog.accept() if answer else dialog.dismiss()

    page.on("dialog", reply)
    page.goto(f"{live_server.url}{reverse('orange_team:dashboard')}")
    return context, page, questions


@pytest.mark.parametrize("answer", [True, False])
def test_checking_out_asks_first(live_server, pw_browser, answer):
    from orange_team.models import OrangeCheckIn

    user = _create_role_user("orange_team", None)
    OrangeCheckIn.objects.create(user=user)
    context, page, questions = open_dashboard(live_server, pw_browser, user, answer)
    try:
        page.get_by_role("button", name="Check Out").click()
        page.wait_for_load_state("networkidle")
        assert questions == ["Check out? You will stop receiving assignments."]
        assert OrangeCheckIn.objects.filter(user=user, is_active=True).exists() is not answer
    finally:
        context.close()


def test_checking_in_asks_nothing(live_server, pw_browser):
    from orange_team.models import OrangeCheckIn

    user = _create_role_user("orange_team", None)
    context, page, questions = open_dashboard(live_server, pw_browser, user, False)
    try:
        with page.expect_navigation():
            page.get_by_role("button", name="Check In").click()
        assert questions == []
        assert OrangeCheckIn.objects.filter(user=user, is_active=True).exists()
    finally:
        context.close()


def test_a_dismissed_category_change_keeps_the_category(live_server, pw_browser):
    from team.models import Team
    from ticketing.models import Ticket, TicketCategory

    user = _create_role_user("admin", None)
    team = Team.objects.create(team_number=7, team_name="Team 07")
    first, second = TicketCategory.objects.all()[:2]
    ticket = Ticket.objects.create(ticket_number="T007-001", team=team, category=first, title="t", status="claimed")
    context = create_session_context(pw_browser, live_server, user)
    page = context.new_page()
    questions: list[str] = []
    page.on("dialog", lambda dialog: (questions.append(dialog.message), dialog.dismiss()))
    try:
        page.goto(f"{live_server.url}{reverse('ticket_detail', kwargs={'ticket_number': ticket.ticket_number})}")
        page.get_by_label("Change ticket category").select_option(str(second.pk))
        page.wait_for_load_state("networkidle")
        assert questions == ["Change category? This may change points charged."]
        ticket.refresh_from_db()
        assert ticket.category_id == first.pk
    finally:
        context.close()


def test_cancelling_a_ticket_asks_first(live_server, pw_browser):
    from ticketing.models import Ticket

    user = _create_role_user("blue_team", None)
    ticket = Ticket.objects.create(ticket_number="T001-009", team=user.discord_links.first().team, title="t")
    context = create_session_context(pw_browser, live_server, user)
    page = context.new_page()
    questions: list[str] = []
    page.on("dialog", lambda dialog: (questions.append(dialog.message), dialog.dismiss()))
    try:
        page.goto(f"{live_server.url}{reverse('ticket_detail', kwargs={'ticket_number': ticket.ticket_number})}")
        page.get_by_role("button", name="Cancel Ticket").click()
        page.wait_for_load_state("networkidle")
        assert questions == ["Are you sure you want to cancel this ticket?"]
        ticket.refresh_from_db()
        assert ticket.status == "open"
    finally:
        context.close()
