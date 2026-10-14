"""An event saves in the admin with every field as the admin form shows it."""

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse

from core.models import UserGroups
from registration.models import Event, Season

pytestmark = pytest.mark.django_db


def test_an_event_saves_in_the_admin():
    admin = User.objects.create(username="admin", is_staff=True, is_superuser=True)
    UserGroups.objects.update_or_create(user=admin, defaults={"groups": ["WCComps_Discord_Admin"], "authentik_id": "x"})
    client = Client()
    client.force_login(admin)
    old = Season.objects.create(name="Old", year=2095)
    new = Season.objects.create(name="New", year=2096)
    event = Event.objects.create(season=old, name="Inv", event_type="invitational", date="2095-10-03")
    url = reverse("admin:registration_event_change", args=[event.pk])
    form = client.get(url).context["adminform"].form
    data = {name: form[name].value() if form[name].value() is not None else "" for name in form.fields}
    data.update(
        {
            "season": new.pk,
            "team_assignments-TOTAL_FORMS": "0",
            "team_assignments-INITIAL_FORMS": "0",
        }
    )

    response = client.post(url, data)

    assert response.status_code == 302, response.context["adminform"].form.errors
    event.refresh_from_db()
    assert event.season == new
