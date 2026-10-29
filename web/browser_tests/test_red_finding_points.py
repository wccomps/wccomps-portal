"""The submit finding page's live total matches what the finding will score for the outcomes ticked."""

import pytest
from django.urls import reverse

from core.tests.roles import role_user

from .conftest import create_session_context

pytestmark = [pytest.mark.browser, pytest.mark.django_db(transaction=True)]

COMBINATIONS = [
    ("root_access", "user_access"),
    ("user_access",),
    ("user_access", "privilege_escalation", "pii_recovered"),
    ("root_access", "user_access", "credentials_recovered", "db_decrypted"),
    (
        "root_access",
        "user_access",
        "privilege_escalation",
        "credentials_recovered",
        "sensitive_files_recovered",
        "credit_cards_recovered",
        "pii_recovered",
        "encrypted_db_recovered",
        "db_decrypted",
    ),
]


def test_the_live_total_is_what_the_finding_scores(live_server, pw_browser):
    from scoring.models import RedTeamScore

    context = create_session_context(pw_browser, live_server, role_user("red_team"))
    page = context.new_page()
    try:
        page.goto(live_server.url + reverse("scoring:submit_red_score"))
        for ticked in COMBINATIONS:
            for outcome in RedTeamScore.OUTCOMES:
                page.locator(f"#id_{outcome.field}").set_checked(outcome.field in ticked)
            expected = RedTeamScore(**dict.fromkeys(ticked, True)).calculate_points()
            assert page.locator("#pointsValue").inner_text() == str(int(expected)), ticked
    finally:
        context.close()
