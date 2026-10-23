"""Inject grading's Clear Form empties every team's points, so a save after it posts none."""

from decimal import Decimal
from unittest.mock import patch
from urllib.parse import parse_qs

import pytest
from django.urls import reverse

from core.tests.roles import role_user

from .conftest import create_session_context

pytestmark = [pytest.mark.browser, pytest.mark.django_db(transaction=True)]


def test_clear_form_empties_the_points_and_a_save_posts_none(live_server, pw_browser):
    from quotient.client import Inject
    from scoring.models import InjectScore

    from team.models import Team

    user = role_user("admin")
    for number in (1, 2):
        team = Team.objects.create(team_number=number, team_name=f"Team {number:02d}", is_active=True)
        InjectScore.objects.create(
            team=team, inject_id="1", inject_name="Inject 01", points_awarded=Decimal(50), graded_by=user
        )
    inject = Inject(inject_id=1, title="Inject 01 - Web (100 pts)", description="")
    context = create_session_context(pw_browser, live_server, user)
    page = context.new_page()
    try:
        with patch("quotient.client.QuotientClient.get_injects", return_value=[inject]):
            page.goto(f"{live_server.url}{reverse('scoring:inject_grading')}?inject=1")
            page.get_by_role("button", name="Clear Form").click()
            points = page.locator('input[name^="points_team_"]')
            assert [points.nth(i).input_value() for i in range(points.count())] == ["", ""]
            with page.expect_request(lambda r: r.method == "POST") as posted:
                page.get_by_role("button", name="Save Grades").click()
            fields = parse_qs(posted.value.post_data or "", keep_blank_values=True)
            assert fields["points_team_1"] == [""] and fields["points_team_2"] == [""]
    finally:
        context.close()
