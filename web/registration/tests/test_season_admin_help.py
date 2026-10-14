"""The season admin page says how to make a season the active one, since Active is read-only there."""

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse

from core.models import UserGroups
from registration.models import Season

pytestmark = pytest.mark.django_db


def test_the_change_page_says_how_to_activate():
    admin = User.objects.create(username="admin", is_staff=True, is_superuser=True)
    UserGroups.objects.update_or_create(user=admin, defaults={"groups": ["WCComps_Discord_Admin"], "authentik_id": "x"})
    client = Client()
    client.force_login(admin)
    season = Season.objects.create(name="Next", year=2091)

    page = client.get(reverse("admin:registration_season_change", args=[season.pk])).content.decode()

    assert "Make the selected season the active one" in page
