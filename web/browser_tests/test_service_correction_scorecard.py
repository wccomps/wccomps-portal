import pytest
from django.core.management import call_command
from django.urls import reverse
from scoring.models import ScoringTemplate
from scoring.tests import test_service_corrections

from core.tests.roles import role_user

from .conftest import create_session_context

pytestmark = [pytest.mark.browser, pytest.mark.django_db(transaction=True)]
scorecard_correction = test_service_corrections.correction


@pytest.mark.parametrize(
    ("scorecard_correction", "change", "composite", "full_void"),
    [((100, b"742,4,5,9\n", False), 4, "9", False), ((100, b"742,-100,5,-95\n", True), -100, "-95", True)],
    indirect=["scorecard_correction"],
)
def test_staff_scorecard_separates_correction_from_observed_history(
    scorecard_correction, change, composite, full_void, live_server, pw_browser
):
    team, _, _, apply, _, _ = scorecard_correction
    call_command("service_correction", "apply", *apply, "--expect-composite", composite)
    ScoringTemplate.objects.create(service_max=400, inject_max=800)
    multiplier = "2.00" if full_void else "1.00"
    context = create_session_context(pw_browser, live_server, role_user("gold_team"))
    try:
        page = context.new_page()
        path = reverse("leaderboard_scorecard", args=[team.team_number])
        assert page.goto(f"{live_server.url}{path}").status == 200
        assert page.get_by_role("heading", name="Service scoring corrections").is_visible()
        assert page.get_by_text("full void,", exact=False).count() == int(full_void)
        assert page.get_by_text(
            f"{change} raw service points at a multiplier of {multiplier}, +5 SLA points restored.", exact=False
        ).is_visible()
        assert page.get_by_text("Service: 100 raw", exact=False).is_visible()
        details = page.get_by_role("table", name="Service uptime scores")
        raw_row = details.get_by_role("row").filter(has_text=test_service_corrections.SERVICE)
        assert raw_row.get_by_role("cell", name="100", exact=True).is_visible()
        assert raw_row.get_by_role("cell", name="1", exact=True).is_visible()
    finally:
        context.close()
