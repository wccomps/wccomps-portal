"""The event admin shows assignments without editing them: they follow the school list."""

from datetime import date

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse

from core.models import UserGroups
from registration.models import Event, EventTeamAssignment, School, Season, TeamRegistration
from team.models import Team

pytestmark = pytest.mark.django_db


@pytest.fixture
def admin_client():
    admin = User.objects.create(username="admin", is_staff=True, is_superuser=True)
    UserGroups.objects.update_or_create(user=admin, defaults={"groups": ["WCComps_Discord_Admin"], "authentik_id": "x"})
    client = Client()
    client.force_login(admin)
    return client


def test_the_event_page_cannot_add_change_or_delete_assignments(admin_client):
    season = Season.objects.create(name="2026", year=2026, is_active=True)
    event = Event.objects.create(
        season=season, name="Inv", event_type="invitational", date=date(2026, 10, 3), is_active=True
    )
    team = Team.objects.create(team_number=4, team_name="Team 04")
    EventTeamAssignment.objects.create(event=event, team=team, school=School.for_name("Example University"))

    page = admin_client.get(reverse("admin:registration_event_change", args=[event.pk])).content.decode()

    assert "Example University" in page
    assert 'name="team_assignments-0-school"' not in page
    assert 'name="team_assignments-0-team"' not in page
    assert 'name="team_assignments-0-DELETE"' not in page
    assert "team_assignments-__prefix__-school" not in page


def test_the_event_page_cannot_open_an_event(admin_client):
    season = Season.objects.create(name="2026", year=2026, is_active=True)
    event = Event.objects.create(season=season, name="Inv", event_type="invitational", date=date(2026, 10, 3))

    page = admin_client.get(reverse("admin:registration_event_change", args=[event.pk])).content.decode()

    assert 'name="is_active"' not in page


def test_the_registration_page_cannot_change_the_school(admin_client):
    school = School.for_name("Example University")
    other = School.for_name("Other College")
    registration = TeamRegistration.objects.create(school_name="Example University", status="paid", school=school)
    url = reverse("admin:registration_teamregistration_change", args=[registration.pk])

    page = admin_client.get(url).content.decode()

    assert 'name="school"' not in page
    form = admin_client.get(url).context["adminform"].form
    data = {name: form[name].value() or "" for name in form.fields}
    data |= {"school": other.pk}
    for prefix in ("contacts", "event_enrollments"):
        data |= {f"{prefix}-TOTAL_FORMS": "0", f"{prefix}-INITIAL_FORMS": "0"}
    response = admin_client.post(url, data)

    assert response.status_code == 302, response.context["adminform"].form.errors if response.context else ""
    registration.refresh_from_db()
    assert registration.school == school
