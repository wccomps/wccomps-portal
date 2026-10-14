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


def test_add_mode_adds_a_late_school_and_keeps_the_list(live_server, pw_browser, tmp_path):
    from datetime import date

    from registration.models import Event, Season
    from registration.services import set_team_school

    from team.models import SchoolInfo, Team

    season = Season.objects.create(name="2026", year=2026, is_active=True)
    event = Event.objects.create(
        season=season, name="Invitational", event_type="invitational", date=date(2026, 10, 3), is_active=True
    )
    first, _ = Team.objects.get_or_create(team_number=1, defaults={"team_name": "Team 01", "is_active": True})
    Team.objects.get_or_create(team_number=2, defaults={"team_name": "Team 02", "is_active": False})
    set_team_school(first, "School One", contact_email="one@example.edu")
    csv_file = tmp_path / "late.csv"
    csv_file.write_text("school_name,contact_email\nSchool One,one@example.edu\nSchool Two,two@example.edu\n")
    user = _create_role_user("gold_team", None)
    context = create_session_context(pw_browser, live_server, user)
    page = context.new_page()
    try:
        page.goto(f"{live_server.url}/ops/school-info/import/")
        page.select_option("select[name=event]", str(event.pk))
        page.check("#id_mode_add")
        page.set_input_files("#id_csv_file", str(csv_file))
        page.get_by_role("button", name="Upload and Preview").click()
        page.wait_for_load_state("networkidle")
        assert page.get_by_text("Already on the list, skipped: School One").is_visible(), page.content()[:2000]

        page.get_by_role("button", name="Confirm and Import").click()
        page.wait_for_load_state("networkidle")
        assert page.get_by_text("Added 1 school(s) to Invitational (2026).").is_visible()
        assert dict(SchoolInfo.objects.values_list("team__team_number", "school__name")) == {
            1: "School One",
            2: "School Two",
        }
    finally:
        context.close()


def test_with_no_active_season_the_import_creates_one(live_server, pw_browser, tmp_path):
    from registration.models import Event, Season

    from team.models import Team

    Team.objects.get_or_create(team_number=1, defaults={"team_name": "Team 01", "is_active": True})
    csv_file = tmp_path / "schools.csv"
    csv_file.write_text(CSV)
    user = _create_role_user("gold_team", None)
    context = create_session_context(pw_browser, live_server, user)
    page = context.new_page()
    try:
        page.goto(f"{live_server.url}/ops/school-info/import/")
        page.select_option("select[name=event]", "new")
        page.fill("#id_new_name", "Invitational 1")
        page.fill("#id_new_date", "2026-10-03")
        page.fill("#id_new_season_name", "2026-2027")
        page.fill("#id_new_season_year", "2027")
        page.set_input_files("#id_csv_file", str(csv_file))
        page.get_by_role("button", name="Upload and Preview").click()
        page.wait_for_load_state("networkidle")
        page.get_by_role("button", name="Confirm and Import").click()
        page.wait_for_load_state("networkidle")

        assert page.get_by_text("Created the 2026-2027 season and made it the active one.").is_visible()
        assert Season.objects.active().year == 2027
        assert Event.objects.open().name == "Invitational 1"
    finally:
        context.close()
