"""The submit finding page's live total matches what the finding will score for its access level and outcomes."""

import pytest
from django.urls import reverse

from core.tests.roles import role_user

from .conftest import create_session_context

pytestmark = [pytest.mark.browser, pytest.mark.django_db(transaction=True)]

COMBINATIONS = [
    ("root", ()),
    ("user", ()),
    ("user", ("privilege_escalation", "pii_recovered")),
    ("root", ("credentials_recovered", "db_decrypted")),
    ("none", ("credentials_recovered",)),
    (
        "root",
        (
            "privilege_escalation",
            "credentials_recovered",
            "sensitive_files_recovered",
            "credit_cards_recovered",
            "pii_recovered",
            "encrypted_db_recovered",
            "db_decrypted",
        ),
    ),
]


def test_the_live_total_is_what_the_finding_scores(live_server, pw_browser):
    from scoring.models import RedTeamScore

    context = create_session_context(pw_browser, live_server, role_user("red_team"))
    page = context.new_page()
    try:
        page.goto(live_server.url + reverse("scoring:submit_red_score"))
        assert page.locator("#pointsValue").inner_text() == "0"
        for level, ticked in COMBINATIONS:
            page.check(f'input[name="access_level"][value="{level}"]')
            for outcome in RedTeamScore.OUTCOMES:
                page.locator(f"#id_{outcome.field}").set_checked(outcome.field in ticked)
            expected = RedTeamScore(access_level=level, **dict.fromkeys(ticked, True)).calculate_points()
            assert page.locator("#pointsValue").inner_text() == str(int(expected)), (level, ticked)
    finally:
        context.close()
