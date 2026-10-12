"""Importing school info from a CSV: upload shows a preview, confirm creates the rows."""

import pytest

from .conftest import _create_role_user, create_session_context

pytestmark = [pytest.mark.browser, pytest.mark.django_db(transaction=True)]

CSV = "school_name,contact_email\nExample University,coach@example.edu\n"


def test_upload_shows_preview_and_confirm_imports(live_server, pw_browser, tmp_path):
    from datetime import date

    from registration.models import Event, Season

    from team.models import SchoolInfo, Team

    season = Season.objects.create(name="2026", year=2026, is_active=True)
    event = Event.objects.create(season=season, name="Invitational", event_type="invitational", date=date(2026, 10, 3))
    Team.objects.get_or_create(team_number=1, defaults={"team_name": "Team 01", "is_active": True})
    csv_file = tmp_path / "schools.csv"
    csv_file.write_text(CSV)
    user = _create_role_user("gold_team", None)
    context = create_session_context(pw_browser, live_server, user)
    page = context.new_page()
    try:
        page.goto(f"{live_server.url}/ops/school-info/import/")
        page.select_option("select[name=event]", str(event.pk))
        page.set_input_files("#id_csv_file", str(csv_file))
        page.get_by_role("button", name="Upload and Preview").click()
        page.wait_for_load_state("networkidle")
        assert page.get_by_text("Example University").first.is_visible(), page.content()[:2000]

        page.get_by_role("button", name="Confirm and Import").click()
        page.wait_for_load_state("networkidle")
        assert SchoolInfo.objects.filter(school__name="Example University").exists()
        assert Event.objects.get(pk=event.pk).is_active
    finally:
        context.close()
