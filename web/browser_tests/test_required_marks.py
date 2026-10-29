"""A required group's legend says "required" to assistive tech only where its options don't already."""

import pytest
from django.urls import reverse

from core.tests.roles import role_user

from .conftest import create_session_context

pytestmark = [pytest.mark.browser, pytest.mark.django_db(transaction=True)]


def test_a_required_group_says_required_once(live_server, pw_browser):
    """The source IP radios carry required themselves; the team picker's buttons don't, so its legend says it."""
    from team.models import Team

    Team.objects.create(team_number=3, team_name="Team 03", is_active=True)
    context = create_session_context(pw_browser, live_server, role_user("red_team"))
    page = context.new_page()
    try:
        page.goto(f"{live_server.url}{reverse('scoring:submit_red_score')}")
        mark_content = dict(
            page.evaluate(
                "Array.from(document.querySelectorAll('fieldset > legend.required'), legend => "
                "[legend.textContent.trim(), getComputedStyle(legend, '::after').content])"
            )
        )
        assert mark_content["Affected Teams:"].endswith('/ " required"')
        assert mark_content["Source IP:"].endswith('/ ""')
    finally:
        context.close()
