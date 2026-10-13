"""The Django admin can't edit a team's stored packet password: it would change only the portal's copy."""

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from registration.models import School

from core.models import UserGroups
from team.models import SchoolInfo, Team

pytestmark = pytest.mark.django_db


def test_admin_save_leaves_the_stored_password_alone():
    team = Team.objects.create(team_number=7, team_name="Team 07")
    info = SchoolInfo.objects.create(
        team=team, school=School.for_name("Example University"), contact_email="c@example.com", password="Real-Pass-1!"
    )
    admin = User.objects.create(username="admin", is_staff=True, is_superuser=True)
    UserGroups.objects.update_or_create(user=admin, defaults={"groups": ["WCComps_Discord_Admin"], "authentik_id": "x"})
    client = Client()
    client.force_login(admin)
    url = reverse("admin:team_schoolinfo_change", args=[info.pk])

    page = client.get(url).content.decode()
    response = client.post(
        url,
        {
            "team": team.pk,
            "contact_email": "c@example.com",
            "secondary_email": "",
            "notes": "",
            "password": "Typed-Here-2!",
        },
    )

    assert 'name="password"' not in page
    assert response.status_code == 302
    info.refresh_from_db()
    assert info.password == "Real-Pass-1!"


def _admin_client(username="admin"):
    admin = User.objects.create(username=username, is_staff=True, is_superuser=True)
    UserGroups.objects.update_or_create(user=admin, defaults={"groups": ["WCComps_Discord_Admin"], "authentik_id": "x"})
    client = Client()
    client.force_login(admin)
    return client


def test_admin_cannot_change_the_school():
    team = Team.objects.create(team_number=7, team_name="Team 07")
    school = School.for_name("Example University")
    other = School.for_name("Other College")
    info = SchoolInfo.objects.create(team=team, school=school, contact_email="c@example.com")
    client = _admin_client()
    url = reverse("admin:team_schoolinfo_change", args=[info.pk])

    page = client.get(url).content.decode()
    response = client.post(
        url,
        {
            "team": team.pk,
            "school": other.pk,
            "school_name": "Renamed College",
            "contact_email": "new@example.com",
            "secondary_email": "",
            "notes": "",
        },
    )

    assert 'name="school"' not in page
    assert 'name="school_name"' not in page
    assert "Example University" in page
    assert response.status_code == 302
    info.refresh_from_db()
    assert info.school == school
    assert info.school_name == "Example University"
    assert info.contact_email == "new@example.com"


def test_admin_searches_by_school_name():
    team = Team.objects.create(team_number=7, team_name="Team 07")
    SchoolInfo.objects.create(team=team, school=School.for_name("Example University"), contact_email="c@example.com")

    page = _admin_client().get(reverse("admin:team_schoolinfo_changelist"), {"q": "example univ"}).content.decode()

    assert "Example University" in page


def test_admin_cannot_add_school_info():
    """A team's school is set on the School Info page or by the import, which keep the event's record with it."""
    assert _admin_client().get("/admin/team/schoolinfo/add/").status_code == 403


@pytest.mark.django_db
@pytest.mark.parametrize("path", ["/admin/team/team/add/"])
def test_add_pages_render_without_a_reset_button(path):
    admin = User.objects.create(username="adminadd", is_staff=True, is_superuser=True)
    UserGroups.objects.update_or_create(user=admin, defaults={"groups": ["WCComps_Discord_Admin"], "authentik_id": "x"})
    client = Client()
    client.force_login(admin)

    response = client.get(path)

    assert response.status_code == 200
    assert b"Reset Password" not in response.content
    assert b'name="password"' not in response.content


def test_admin_cannot_delete_school_info():
    team = Team.objects.create(team_number=7, team_name="Team 07")
    info = SchoolInfo.objects.create(team=team, school=School.for_name("Example University"), contact_email="c@e.com")
    client = _admin_client()

    delete_page = client.get(reverse("admin:team_schoolinfo_delete", args=[info.pk]))
    changelist = client.get(reverse("admin:team_schoolinfo_changelist")).content.decode()

    assert delete_page.status_code == 403
    assert 'value="delete_selected"' not in changelist
    assert SchoolInfo.objects.filter(pk=info.pk).exists()


def test_admin_cannot_move_school_info_to_another_team():
    team = Team.objects.create(team_number=7, team_name="Team 07")
    other = Team.objects.create(team_number=8, team_name="Team 08")
    info = SchoolInfo.objects.create(team=team, school=School.for_name("Example University"), contact_email="c@e.com")
    client = _admin_client()
    url = reverse("admin:team_schoolinfo_change", args=[info.pk])

    page = client.get(url).content.decode()
    response = client.post(url, {"team": other.pk, "contact_email": "c@e.com", "secondary_email": "", "notes": ""})

    assert 'name="team"' not in page
    assert response.status_code == 302
    info.refresh_from_db()
    assert info.team == team


def test_admin_save_writes_only_the_contact_fields():
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    team = Team.objects.create(team_number=7, team_name="Team 07")
    info = SchoolInfo.objects.create(team=team, school=School.for_name("Example University"), contact_email="c@e.com")
    client = _admin_client()
    url = reverse("admin:team_schoolinfo_change", args=[info.pk])

    with CaptureQueriesContext(connection) as queries:
        client.post(url, {"contact_email": "new@e.com", "secondary_email": "", "notes": "late"})

    updates = [q["sql"] for q in queries.captured_queries if q["sql"].startswith('UPDATE "team_schoolinfo"')]
    assert len(updates) == 1
    for column in ('"password"', '"school_id"', '"team_id"'):
        assert column not in updates[0]
    info.refresh_from_db()
    assert (info.contact_email, info.notes) == ("new@e.com", "late")


def test_admin_cannot_delete_a_team():
    """Teams are fixed slots from init_teams; deleting one would take its season history with it."""
    team = Team.objects.create(team_number=7, team_name="Team 07")
    client = _admin_client()

    delete_page = client.get(reverse("admin:team_team_delete", args=[team.pk]))
    changelist = client.get(reverse("admin:team_team_changelist")).content.decode()

    assert delete_page.status_code == 403
    assert 'value="delete_selected"' not in changelist
    assert Team.objects.filter(pk=team.pk).exists()
