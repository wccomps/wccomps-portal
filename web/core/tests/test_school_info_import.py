"""School info CSV import: upload previews, confirm imports, and failures say why."""

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse

from team.models import SchoolInfo, Team

pytestmark = pytest.mark.django_db


@pytest.fixture
def client(gold_team_user):
    client = Client()
    client.force_login(gold_team_user)
    return client


def _season_and_event(name="Invitational", active=False):
    from datetime import date

    from registration.models import Event, Season

    season, _ = Season.objects.get_or_create(year=2026, defaults={"name": "2026", "is_active": True})
    return Event.objects.create(
        season=season, name=name, event_type="invitational", date=date(2026, 10, 3), is_active=active
    )


def test_upload_then_confirm_imports(client):
    Team.objects.create(team_number=1, team_name="Team 01", is_active=True)
    event = _season_and_event()
    csv = SimpleUploadedFile("schools.csv", b"school_name,contact_email\nExample University,coach@example.edu\n")

    preview = client.post(reverse("school_info_import"), {"upload": "true", "csv_file": csv, "event": str(event.pk)})
    assert b"Example University" in preview.content

    client.post(reverse("school_info_import"), {"confirm": "true"})
    assert SchoolInfo.objects.filter(school_name="Example University").exists()


def test_confirm_after_the_upload_expired_says_so(client):
    response = client.post(reverse("school_info_import"), {"confirm": "true"})
    assert b"no longer available" in response.content


def test_import_for_an_existing_event_opens_it(client):
    from registration.models import Event

    Team.objects.create(team_number=1, team_name="Team 01", is_active=True)
    event = _season_and_event()
    csv = SimpleUploadedFile("schools.csv", b"school_name,contact_email\nExample University,coach@example.edu\n")

    client.post(reverse("school_info_import"), {"upload": "true", "csv_file": csv, "event": str(event.pk)})
    client.post(reverse("school_info_import"), {"confirm": "true"})

    assert Event.objects.get(pk=event.pk).is_active


def test_import_can_create_the_event(client):
    from registration.models import Event

    Team.objects.create(team_number=1, team_name="Team 01", is_active=True)
    _season_and_event(name="Earlier")
    csv = SimpleUploadedFile("schools.csv", b"school_name,contact_email\nExample University,coach@example.edu\n")
    fields = {"event": "new", "new_name": "Invitational 2", "new_date": "2026-11-07", "new_type": "invitational"}

    client.post(reverse("school_info_import"), {"upload": "true", "csv_file": csv, **fields})
    client.post(reverse("school_info_import"), {"confirm": "true"})

    assert Event.objects.get(is_active=True).name == "Invitational 2"


def test_import_for_another_event_is_refused_while_one_is_open(client):
    Team.objects.create(team_number=1, team_name="Team 01", is_active=True)
    _season_and_event(name="Open", active=True)
    other = _season_and_event(name="Next")
    csv = SimpleUploadedFile("schools.csv", b"school_name,contact_email\nExample University,coach@example.edu\n")

    response = client.post(reverse("school_info_import"), {"upload": "true", "csv_file": csv, "event": str(other.pk)})

    assert b"is still open" in response.content
    assert not SchoolInfo.objects.exists()
