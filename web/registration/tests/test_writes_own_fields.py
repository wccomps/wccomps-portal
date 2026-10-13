"""Each writer saves only the fields it owns, so a stale copy can't put back a school, password or open event."""

from datetime import date

import pytest
from django.contrib.auth.models import User
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from core.models import UserGroups
from registration.models import Event, School, Season, TeamRegistration
from registration.services import set_team_school
from team.models import SchoolInfo, Team

pytestmark = pytest.mark.django_db


def _updates(queries, table):
    return [q["sql"] for q in queries.captured_queries if q["sql"].startswith(f'UPDATE "{table}"')]


@pytest.mark.parametrize("change", ["approve", "reject", "mark_as_paid"])
def test_a_stale_registration_keeps_the_school_set_meanwhile(change):
    registration = TeamRegistration.objects.create(school_name="Example University", status="paid")
    stale = TeamRegistration.objects.get(pk=registration.pk)
    school = School.for_name("Example University")
    TeamRegistration.objects.filter(pk=registration.pk).update(school=school)

    match change:
        case "approve":
            stale.approve(User.objects.create(username="gold"))
        case "reject":
            stale.reject("late")
        case "mark_as_paid":
            stale.mark_as_paid()

    registration.refresh_from_db()
    assert registration.school == school


def test_setting_a_school_never_writes_the_stored_password():
    team = Team.objects.create(team_number=4, team_name="Team 04")
    set_team_school(team, "Example University", contact_email="c@example.edu")

    with CaptureQueriesContext(connection) as queries:
        set_team_school(team, "Example University", contact_email="new@example.edu")

    updates = _updates(queries, "team_schoolinfo")
    assert updates
    assert all('"password"' not in sql for sql in updates)
    assert SchoolInfo.objects.get().contact_email == "new@example.edu"


@pytest.fixture
def event():
    season = Season.objects.create(name="2026", year=2026, is_active=True)
    return Event.objects.create(season=season, name="Inv", event_type="invitational", date=date(2026, 10, 3))


def test_the_event_edit_page_never_writes_is_active(gold_team_user, event):
    client = Client()
    client.force_login(gold_team_user)

    with CaptureQueriesContext(connection) as queries:
        response = client.post(
            reverse("registration_event_edit", args=[event.pk]),
            {
                "name": "Renamed",
                "event_type": "invitational",
                "date": "2026-10-03",
                "start_time": "09:00",
                "end_time": "17:00",
                "registration_open": "on",
                "max_teams": "50",
            },
        )

    assert response.status_code == 302
    updates = _updates(queries, "registration_event")
    assert updates
    assert all('"is_active"' not in sql for sql in updates)
    event.refresh_from_db()
    assert event.name == "Renamed"


def _post_event_admin(event, **changes):
    """Save the event's admin change page with the given fields changed; returns the event UPDATEs."""
    admin = User.objects.create(username="admin", is_staff=True, is_superuser=True)
    UserGroups.objects.update_or_create(user=admin, defaults={"groups": ["WCComps_Discord_Admin"], "authentik_id": "x"})
    client = Client()
    client.force_login(admin)
    url = reverse("admin:registration_event_change", args=[event.pk])
    form = client.get(url).context["adminform"].form
    data = {name: form[name].value() for name in form.fields}
    data |= {
        "name": "Renamed",
        "date": "2026-10-03",
        "reminder_days": "[7]",
        "registration_deadline_0": "",
        "registration_deadline_1": "",
        "last_reminder_sent_0": "",
        "last_reminder_sent_1": "",
        "team_assignments-TOTAL_FORMS": "0",
        "team_assignments-INITIAL_FORMS": "0",
    }
    data = {k: v for k, v in data.items() if v is not None and v is not False} | changes

    with CaptureQueriesContext(connection) as queries:
        response = client.post(url, data)

    assert response.status_code == 302, response.context["adminform"].form.errors if response.context else ""
    updates = _updates(queries, "registration_event")
    assert updates
    event.refresh_from_db()
    assert event.name == "Renamed"
    return updates


def test_the_event_admin_never_writes_is_active(event):
    updates = _post_event_admin(event)

    assert all('"is_active"' not in sql for sql in updates)


def test_the_event_admin_never_writes_is_finalized(event):
    """Only Finalize writes it, under the school list lock; a ticked box or a stale page must not."""
    updates = _post_event_admin(event, is_finalized="on")

    assert all('"is_finalized"' not in sql for sql in updates)
    assert not event.is_finalized
