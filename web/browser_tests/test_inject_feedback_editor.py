"""Inject feedback's in-place editor: the row's own Save posts the edit without asking about unsaved changes, Approve
waits until the edit is saved, and a discarded edit puts the stored text back so a later Save doesn't send it."""

from decimal import Decimal

import pytest
from django.urls import reverse
from playwright.sync_api import expect

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
    expect(editor).to_be_hidden()
    assert editor.input_value() == "Good work"

    with page.expect_navigation():
        page.get_by_role("button", name="Save", exact=True).click()
    score.refresh_from_db()
    assert score.feedback == "Good work"


def _approve_selected_with_a_draft(page, *, discard: bool) -> tuple[list[str], list[str]]:
    """Check the row, edit it, and press Approve Selected, answering the discard question with `discard` and the
    approve question yes. The questions asked and the POSTs sent."""
    questions: list[str] = []
    posts: list[str] = []

    def reply(dialog):
        questions.append(dialog.message)
        dialog.accept() if discard or not dialog.message.endswith("Discard?") else dialog.dismiss()

    page.on("dialog", reply)
    page.on("request", lambda request: posts.append(request.url) if request.method == "POST" else None)
    page.get_by_role("checkbox", name="Select Inject 1 Team 1").check()
    _edit(page, "Draft to keep")
    return questions, posts


def test_keeping_a_draft_stops_approve_selected(page, score):
    questions, posts = _approve_selected_with_a_draft(page, discard=False)
    page.get_by_role("button", name="Approve Selected").click()
    page.wait_for_load_state("networkidle")
    assert questions == ["You have unsaved changes. Discard?"]
    assert posts == []
    editor = page.get_by_role("textbox", name="Feedback for Inject 1 Team 1")
    assert editor.input_value() == "Draft to keep"
    score.refresh_from_db()
    assert not score.feedback_approved


def test_discarding_a_draft_lets_approve_selected_go_on(page, score):
    questions, posts = _approve_selected_with_a_draft(page, discard=True)
    with page.expect_navigation():
        page.get_by_role("button", name="Approve Selected").click()
    assert questions == ["You have unsaved changes. Discard?", "Approve 1 feedback item(s)? This cannot be undone."]
    assert posts == [f"{page.url.split('/scoring/')[0]}{reverse('scoring:bulk_approve_inject_feedback')}"]
    score.refresh_from_db()
    assert score.feedback_approved
    assert score.feedback == "Good work"
