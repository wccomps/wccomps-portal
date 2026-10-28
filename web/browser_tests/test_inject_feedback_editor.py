"""Inject feedback's in-place editor: the row's own Save posts the edit without asking about unsaved changes, Approve
waits until the edit is saved, and a discarded edit puts the stored text back so a later Save doesn't send it."""

from decimal import Decimal

import pytest
from django.urls import reverse

from core.tests.roles import role_user

from .conftest import create_session_context

pytestmark = [pytest.mark.browser, pytest.mark.django_db(transaction=True)]


@pytest.fixture
def score(db):
    from scoring.models import InjectScore

    from team.models import Team

    team = Team.objects.create(team_number=1, team_name="Team 01", is_active=True)
    return InjectScore.objects.create(
        team=team,
        inject_id="1",
        inject_name="Inject 1",
        max_points=Decimal("100"),
        points_awarded=Decimal("80"),
        feedback="Good work",
        is_approved=True,
    )


@pytest.fixture
def page(live_server, pw_browser, score):
    context = create_session_context(pw_browser, live_server, role_user("admin"))
    page = context.new_page()
    page.goto(f"{live_server.url}{reverse('scoring:review_inject_feedback')}")
    yield page
    context.close()


def _edit(page, text: str):
    page.get_by_role("button", name="Good work").click()
    editor = page.get_by_role("textbox", name="Feedback for Inject 1 Team 1")
    editor.fill(text)
    return editor


def test_the_rows_save_posts_the_edit_without_asking(page, score):
    questions: list[str] = []
    page.on("dialog", lambda dialog: (questions.append(dialog.message), dialog.dismiss()))
    _edit(page, "Better work")
    with page.expect_navigation():
        page.get_by_role("button", name="Save", exact=True).click()
    score.refresh_from_db()
    assert questions == []
    assert score.feedback == "Better work"


def test_approve_waits_until_the_edit_is_saved(page):
    approve = page.get_by_role("button", name="Approve", exact=True)
    assert approve.is_enabled()
    editor = _edit(page, "Better work")
    assert approve.is_disabled()
    editor.fill("Good work")
    assert approve.is_enabled()


def test_a_discarded_edit_is_not_saved_later(page, score):
    answer = {"accept": False}
    page.on("dialog", lambda dialog: dialog.accept() if answer["accept"] else dialog.dismiss())
    _edit(page, "Draft to throw away")
    # A hidden textarea has no role, so the closed editor is found by its name
    editor = page.locator('textarea[name="feedback"]')

    page.get_by_role("columnheader", name="Inject").click()
    assert editor.is_visible()
    assert editor.input_value() == "Draft to throw away"

    answer["accept"] = True
    page.get_by_role("columnheader", name="Inject").click()
    assert editor.is_hidden()
    assert editor.input_value() == "Good work"

    with page.expect_navigation():
        page.get_by_role("button", name="Save", exact=True).click()
    score.refresh_from_db()
    assert score.feedback == "Good work"
