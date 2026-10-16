"""Removing the first rubric row and saving keeps grading on the rows that stay."""

import pytest

from core.tests.roles import role_user

from .conftest import create_session_context

pytestmark = [pytest.mark.browser, pytest.mark.django_db(transaction=True)]


def test_remove_first_criterion_and_save(live_server, pw_browser):
    from orange_team.models import OrangeAssignment, OrangeAssignmentResult, OrangeCheck, OrangeCheckCriterion

    from team.models import Team

    user = role_user("gold_team")
    check = OrangeCheck.objects.create(title="Phones", description="", created_by=user)
    first = OrangeCheckCriterion.objects.create(orange_check=check, label="First", points=1, sort_order=0)
    second = OrangeCheckCriterion.objects.create(orange_check=check, label="Second", points=2, sort_order=1)
    team = Team.objects.create(team_number=9, team_name="Team 09")
    assignment = OrangeAssignment.objects.create(orange_check=check, user=user, team=team)
    OrangeAssignmentResult.objects.create(assignment=assignment, criterion=first, met=True)
    OrangeAssignmentResult.objects.create(assignment=assignment, criterion=second, met=True)

    context = create_session_context(pw_browser, live_server, user)
    page = context.new_page()
    errors: list[str] = []
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
    page.on("dialog", lambda dialog: dialog.accept())

    try:
        page.goto(f"{live_server.url}/orange-team/checks/{check.pk}/edit/")
        assert page.input_value('input[name="criterion_label_1"]') == "Second"

        page.get_by_role("button", name="Remove").first.click()
        page.get_by_role("button", name="Add Criterion").click()
        page.fill('input[name="criterion_label_2"]', "Third")
        page.fill('input[name="criterion_points_2"]', "3")
        with page.expect_navigation():
            page.get_by_role("button", name="Save Changes").click()

        assert list(check.criteria.values_list("label", flat=True)) == ["Second", "Third"]
        results = {r.criterion.label: r.met for r in assignment.results.select_related("criterion")}
        assert results == {"Second": True, "Third": False}
        assert not errors, errors
    finally:
        context.close()


def test_remove_deletes_the_row_clicked(live_server, pw_browser):
    from orange_team.models import OrangeCheck, OrangeCheckCriterion

    user = role_user("gold_team")
    check = OrangeCheck.objects.create(title="Phones", description="", created_by=user)
    for order, label in enumerate(["First", "Second", "Third"]):
        OrangeCheckCriterion.objects.create(orange_check=check, label=label, points=1, sort_order=order)

    context = create_session_context(pw_browser, live_server, user)
    page = context.new_page()
    page.on("dialog", lambda dialog: dialog.accept())
    try:
        page.goto(f"{live_server.url}/orange-team/checks/{check.pk}/edit/")
        page.get_by_role("button", name="Remove").nth(1).click()
        labels = page.locator('input[name^="criterion_label_"]')
        assert [labels.nth(i).input_value() for i in range(labels.count())] == ["First", "Third"]
    finally:
        context.close()
