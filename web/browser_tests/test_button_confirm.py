"""A fetch-posting page asks its question through data-confirm: a button's (or a form's) question is asked before its
Alpine handler posts, as written or as bound from the page's state, and an answer of no posts nothing."""

from unittest.mock import patch

import pytest
from django.urls import reverse

from core.tests.roles import role_user

from .conftest import create_session_context

pytestmark = [pytest.mark.browser, pytest.mark.django_db(transaction=True)]


@pytest.fixture
def team_page(live_server, pw_browser):
    from team.models import Team

    Team.objects.create(team_number=8, team_name="Team 08", is_active=True)
    context = create_session_context(pw_browser, live_server, role_user("admin"))
    page = context.new_page()
    posts: list[str] = []
    page.on("request", lambda request: posts.append(request.url) if request.method == "POST" else None)
    page.goto(live_server.url + reverse("admin_team_detail", args=[8]))
    yield page, posts
    context.close()


def test_a_button_question_answered_no_posts_nothing(team_page):
    page, posts = team_page
    questions: list[str] = []

    def dismiss(dialog):
        questions.append(dialog.message)
        dialog.dismiss()

    page.on("dialog", dismiss)
    page.get_by_role("button", name="Recreate Discord Channels").click()
    page.wait_for_timeout(500)
    assert questions == ["Are you sure you want to recreate Discord channels for this team?"]
    assert posts == []


def test_a_bound_question_is_asked_and_yes_posts(team_page):
    page, posts = team_page
    questions: list[str] = []

    def accept(dialog):
        questions.append(dialog.message)
        dialog.accept()

    page.on("dialog", accept)
    with (
        patch("core.admin_views.teams.reset_team_password", return_value=("Team-Pass-7!", "")),
        patch("core.admin_views.teams.AuthentikManager") as manager,
    ):
        manager.return_value.revoke_user_sessions.return_value = (True, None, 1)
        with page.expect_response(lambda r: r.url.endswith("/action/")):
            page.get_by_role("button", name="Reset Team").click()
    assert questions == [
        "Reset this team? This will:\n- Unlink all members\n- Generate a new Authentik password\n"
        "- Revoke all active sessions\n\nThis cannot be undone."
    ]
    assert len(posts) == 1


def test_a_form_question_follows_what_the_form_holds(live_server, pw_browser):
    context = create_session_context(pw_browser, live_server, role_user("admin"))
    page = context.new_page()
    questions: list[str] = []

    def dismiss(dialog):
        questions.append(dialog.message)
        dialog.dismiss()

    page.on("dialog", dismiss)
    try:
        page.goto(live_server.url + reverse("admin_broadcast"))
        page.locator("select[name=target]").select_option("custom")
        page.locator("input[name=team_numbers]").fill("3,4")
        page.locator("textarea[name=message]").fill("Hello")
        page.get_by_role("button", name="Send Broadcast").click()
        page.wait_for_timeout(500)
        assert questions == ["Send this message to teams 3,4?"]
    finally:
        context.close()
