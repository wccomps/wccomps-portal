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
    assert SchoolInfo.objects.filter(school__name="Example University").exists()


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


def test_confirm_refuses_an_event_finalized_since_the_preview(client):
    Team.objects.create(team_number=1, team_name="Team 01", is_active=True)
    event = _season_and_event()
    csv = SimpleUploadedFile("schools.csv", b"school_name,contact_email\nExample University,coach@example.edu\n")
    client.post(reverse("school_info_import"), {"upload": "true", "csv_file": csv, "event": str(event.pk)})
    event.is_finalized = True
    event.save()

    response = client.post(reverse("school_info_import"), {"confirm": "true"})

    assert b"finalized or removed" in response.content
    event.refresh_from_db()
    assert not event.is_active
    assert not SchoolInfo.objects.exists()


def test_confirm_refuses_an_event_removed_since_the_preview(client):
    Team.objects.create(team_number=1, team_name="Team 01", is_active=True)
    event = _season_and_event()
    csv = SimpleUploadedFile("schools.csv", b"school_name,contact_email\nExample University,coach@example.edu\n")
    client.post(reverse("school_info_import"), {"upload": "true", "csv_file": csv, "event": str(event.pk)})
    event.delete()

    response = client.post(reverse("school_info_import"), {"confirm": "true"})

    assert response.status_code == 200
    assert b"finalized or removed" in response.content


def test_confirm_with_session_data_from_before_events_asks_to_upload_again(client):
    Team.objects.create(team_number=1, team_name="Team 01", is_active=True)
    session = client.session
    session["csv_import_data"] = {
        "teams_to_create": [
            {"team_number": 1, "school_name": "Example University", "contact_email": "coach@example.edu"}
        ]
    }
    session.save()

    response = client.post(reverse("school_info_import"), {"confirm": "true"})

    assert response.status_code == 200
    assert b"Upload the file again" in response.content
    assert not SchoolInfo.objects.exists()


def test_import_is_refused_while_the_last_events_scores_are_still_here(client):
    from scoring.models import ServiceScore

    team = Team.objects.create(team_number=1, team_name="Team 01", is_active=True)
    event = _season_and_event()
    ServiceScore.objects.create(team=team, service_points=5)
    csv = SimpleUploadedFile("schools.csv", b"school_name,contact_email\nExample University,coach@example.edu\n")

    response = client.post(reverse("school_info_import"), {"upload": "true", "csv_file": csv, "event": str(event.pk)})

    assert b"Scores from the last event are still in the portal" in response.content
    assert not SchoolInfo.objects.exists()


def test_confirm_is_refused_when_scores_appeared_since_the_preview(client):
    from scoring.models import ServiceScore

    team = Team.objects.create(team_number=1, team_name="Team 01", is_active=True)
    event = _season_and_event()
    csv = SimpleUploadedFile("schools.csv", b"school_name,contact_email\nExample University,coach@example.edu\n")
    client.post(reverse("school_info_import"), {"upload": "true", "csv_file": csv, "event": str(event.pk)})
    ServiceScore.objects.create(team=team, service_points=5)

    response = client.post(reverse("school_info_import"), {"confirm": "true"})

    assert b"Scores from the last event are still in the portal" in response.content
    assert not SchoolInfo.objects.exists()


def test_confirm_that_loses_the_race_to_open_an_event_says_so(client):
    from unittest.mock import patch

    from registration.models import Event

    Team.objects.create(team_number=1, team_name="Team 01", is_active=True)
    event = _season_and_event()
    csv = SimpleUploadedFile("schools.csv", b"school_name,contact_email\nExample University,coach@example.edu\n")
    client.post(reverse("school_info_import"), {"upload": "true", "csv_file": csv, "event": str(event.pk)})
    other = _season_and_event(name="Other")

    def another_import_opened_first(self):
        # The other confirm committed its event between our checks and our open
        Event.objects.filter(pk=other.pk).update(is_active=True)
        Event.objects.filter(pk=self.pk).update(is_active=True)

    with patch.object(Event, "open", another_import_opened_first):
        response = client.post(reverse("school_info_import"), {"confirm": "true"})

    assert response.status_code == 200
    assert b"Another import opened an event at the same time" in response.content
    assert not SchoolInfo.objects.exists()
    assert list(Event.objects.filter(is_active=True)) == []


def test_reimporting_the_open_event_is_refused_while_its_scores_are_live(client):
    from scoring.models import ServiceScore

    team = Team.objects.create(team_number=1, team_name="Team 01", is_active=True)
    event = _season_and_event(active=True)
    ServiceScore.objects.create(team=team, service_points=5)
    csv = SimpleUploadedFile("schools.csv", b"school_name,contact_email\nExample University,coach@example.edu\n")

    response = client.post(reverse("school_info_import"), {"upload": "true", "csv_file": csv, "event": str(event.pk)})

    assert f"Scores are already recorded for {event}".encode() in response.content
    assert b"Re-importing reassigns team numbers under them" in response.content
    assert not SchoolInfo.objects.exists()


def test_confirming_a_reimport_of_the_open_event_is_refused_once_scores_appear(client):
    from scoring.models import ServiceScore

    team = Team.objects.create(team_number=1, team_name="Team 01", is_active=True)
    event = _season_and_event(active=True)
    csv = SimpleUploadedFile("schools.csv", b"school_name,contact_email\nExample University,coach@example.edu\n")
    client.post(reverse("school_info_import"), {"upload": "true", "csv_file": csv, "event": str(event.pk)})
    ServiceScore.objects.create(team=team, service_points=5)

    response = client.post(reverse("school_info_import"), {"confirm": "true"})

    assert f"Scores are already recorded for {event}".encode() in response.content
    assert not SchoolInfo.objects.exists()


def test_the_session_keeps_the_raw_event_fields(client):
    Team.objects.create(team_number=1, team_name="Team 01", is_active=True)
    _season_and_event()
    csv = SimpleUploadedFile("schools.csv", b"school_name,contact_email\nExample University,coach@example.edu\n")
    fields = {"event": "new", "new_name": "Invitational 2", "new_date": "2026-11-07", "new_type": "invitational"}

    client.post(reverse("school_info_import"), {"upload": "true", "csv_file": csv, **fields})

    assert client.session["csv_import_data"]["event"] == fields


def test_confirm_rechecks_a_new_events_fields(client):
    from registration.models import Event

    Team.objects.create(team_number=1, team_name="Team 01", is_active=True)
    _season_and_event()
    csv = SimpleUploadedFile("schools.csv", b"school_name,contact_email\nExample University,coach@example.edu\n")
    fields = {"event": "new", "new_name": "Invitational 2", "new_date": "2026-11-07", "new_type": "invitational"}
    client.post(reverse("school_info_import"), {"upload": "true", "csv_file": csv, **fields})
    session = client.session
    session["csv_import_data"]["event"]["new_type"] = "bogus"
    session.save()

    response = client.post(reverse("school_info_import"), {"confirm": "true"})

    assert b"Upload the file again" in response.content
    assert not Event.objects.filter(name="Invitational 2").exists()
    assert not SchoolInfo.objects.exists()
