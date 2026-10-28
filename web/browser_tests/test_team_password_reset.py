"""The team page's Reset Password shows the new password and keeps it on screen."""

from unittest.mock import patch

import pytest
from django.urls import reverse

from core.authentik_utils import CredentialReset
from core.tests.roles import role_user

from .conftest import create_session_context

pytestmark = [pytest.mark.browser, pytest.mark.django_db(transaction=True)]


def test_reset_password_shows_the_new_password_without_reloading(live_server, pw_browser):
    from team.models import Team

    Team.objects.create(team_number=8, team_name="Team 08", is_active=True)
    user = role_user("admin")
    context = create_session_context(pw_browser, live_server, user)
    page = context.new_page()
    page.on("dialog", lambda dialog: dialog.accept())

    try:
        with patch(
            "core.admin_views.teams.reset_team_credentials", return_value=CredentialReset(password="New-Pass-9!")
        ):
            page.goto(live_server.url + reverse("admin_team_detail", args=[8]))
            with page.expect_response(lambda r: r.url.endswith("/action/")):
                page.get_by_role("button", name="Reset Password").click()
            page.get_by_text("New-Pass-9!").wait_for()
            page.wait_for_timeout(4000)
            assert page.get_by_text("New-Pass-9!").is_visible(), (
                "the password must stay up (Reset Team reloads after 3s)"
            )
    finally:
        context.close()


def test_reset_team_keeps_the_password_up_and_hides_the_unlinked_members(live_server, pw_browser):
    from django.contrib.auth.models import User

    from team.models import DiscordLink, Team

    team = Team.objects.create(team_number=8, team_name="Team 08", is_active=True)
    member = User.objects.create(username="member08c")
    DiscordLink.objects.create(
        user=member, discord_id=4444, discord_username="memberdiscord", team=team, is_active=True
    )
    user = role_user("admin")
    context = create_session_context(pw_browser, live_server, user)
    page = context.new_page()
    page.on("dialog", lambda dialog: dialog.accept())

    try:
        with patch(
            "core.admin_views.teams.reset_team_credentials", return_value=CredentialReset(password="Team-Pass-7!")
        ):
            page.goto(live_server.url + reverse("admin_team_detail", args=[8]))
            assert page.get_by_text("memberdiscord").is_visible()
            with page.expect_response(lambda r: r.url.endswith("/action/")):
                page.get_by_role("button", name="Reset Team").click()
            page.get_by_text("Team-Pass-7!").wait_for()
            page.wait_for_timeout(4000)
            assert page.get_by_text("Team-Pass-7!").is_visible(), (
                "Reset Team used to reload after 3s, wiping the password"
            )
            assert not page.get_by_text("memberdiscord").is_visible()
            assert page.get_by_text("All members were unlinked").is_visible()
    finally:
        context.close()


@pytest.mark.parametrize(
    "page_path", ["/admin/team/team/{team_pk}/change/", "/admin/team/schoolinfo/{school_pk}/change/"]
)
def test_django_admin_team_pages_have_reset_password(live_server, pw_browser, page_path):
    from django.contrib.auth.models import User
    from registration.models import School

    from core.models import UserGroups
    from team.models import SchoolInfo, Team

    team = Team.objects.create(team_number=8, team_name="Team 08", is_active=True)
    school = SchoolInfo.objects.create(
        team=team, school=School.for_name("Example School"), contact_email="captain@example.com"
    )
    admin = User.objects.create(username="adminreset")
    UserGroups.objects.update_or_create(user=admin, defaults={"groups": ["WCComps_Discord_Admin"], "authentik_id": "x"})
    context = create_session_context(pw_browser, live_server, admin)
    page = context.new_page()
    page.on("dialog", lambda dialog: dialog.accept())

    try:
        with patch(
            "core.admin_views.teams.reset_team_credentials", return_value=CredentialReset(password="New-Pass-9!")
        ) as reset:
            page.goto(live_server.url + page_path.format(team_pk=team.pk, school_pk=school.pk))
            with page.expect_response(lambda r: r.url.endswith("/action/")):
                page.get_by_role("button", name="Reset Password").click()
            page.get_by_text("New-Pass-9!").wait_for()
            reset.assert_called_once_with(8)
            assert page.url.endswith("/change/"), "the click must not submit the admin form"
    finally:
        context.close()


def test_competition_reset_passwords_keeps_the_csv_and_its_download(live_server, pw_browser):
    """The passwords are already changed in Authentik when the CSV shows: a reload would lose the only copy."""
    from team.models import Team

    Team.objects.create(team_number=3, team_name="Team 03", is_active=True)
    user = role_user("admin")
    context = create_session_context(pw_browser, live_server, user)
    page = context.new_page()
    page.on("dialog", lambda dialog: dialog.accept())

    try:
        with patch("core.authentik_utils.reset_team_credentials", return_value=CredentialReset(password="Csv-Pass-3!")):
            page.goto(live_server.url + reverse("admin_competition"))
            page.get_by_role("button", name="Advanced: reset team passwords").click()
            page.get_by_label("Team Numbers").fill("3")
            with page.expect_response(lambda r: r.url.endswith("/action/")):
                page.get_by_role("button", name="Reset Passwords").click()
            csv = page.locator("textarea[readonly]")
            csv.wait_for()
            page.wait_for_timeout(4000)
            assert "Csv-Pass-3!" in csv.input_value(), "the CSV must stay up"
            with page.expect_download() as download:
                page.get_by_role("button", name="Download CSV").click()
            assert download.value.suggested_filename == "blueteam_passwords.csv"
    finally:
        context.close()
