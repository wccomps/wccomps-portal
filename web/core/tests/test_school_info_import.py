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


def _confirm(client):
    """Confirm the latest preview, posting its token as the preview page does."""
    previews = client.session.get("school_list_previews", {})
    token = list(previews)[-1] if previews else ""
    return client.post(reverse("school_info_import"), {"confirm": "true", "preview": token})


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

    _confirm(client)
    assert SchoolInfo.objects.filter(school__name="Example University").exists()


def test_confirm_after_the_upload_expired_says_so(client):
    response = _confirm(client)
    assert b"no longer available" in response.content


@pytest.mark.parametrize(
    "stored",
    [
        {"teams_to_create": "rows", "event": {}},
        {"teams_to_create": [{"team": 1}], "event": {}},
        {"teams_to_create": [], "event": "event"},
    ],
)
def test_confirm_with_a_malformed_stored_upload_asks_for_it_again(client, stored):
    session = client.session
    session["school_list_previews"] = {"token": stored}
    session.save()

    response = _confirm(client)

    assert b"The uploaded file is no longer available." in response.content
    assert b"Upload the file again." in response.content


def test_import_for_an_existing_event_opens_it(client):
    from registration.models import Event

    Team.objects.create(team_number=1, team_name="Team 01", is_active=True)
    event = _season_and_event()
    csv = SimpleUploadedFile("schools.csv", b"school_name,contact_email\nExample University,coach@example.edu\n")

    client.post(reverse("school_info_import"), {"upload": "true", "csv_file": csv, "event": str(event.pk)})
    _confirm(client)

    assert Event.objects.get(pk=event.pk).is_active


def test_import_can_create_the_event(client):
    from registration.models import Event

    Team.objects.create(team_number=1, team_name="Team 01", is_active=True)
    _season_and_event(name="Earlier")
    csv = SimpleUploadedFile("schools.csv", b"school_name,contact_email\nExample University,coach@example.edu\n")
    fields = {"event": "new", "new_name": "Invitational 2", "new_date": "2026-11-07", "new_type": "invitational"}

    client.post(reverse("school_info_import"), {"upload": "true", "csv_file": csv, **fields})
    _confirm(client)

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

    response = _confirm(client)

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

    response = _confirm(client)

    assert response.status_code == 200
    assert b"finalized or removed" in response.content


def test_confirm_with_session_data_from_before_events_asks_to_upload_again(client):
    Team.objects.create(team_number=1, team_name="Team 01", is_active=True)
    session = client.session
    session["school_list_previews"] = {
        "token": {
            "teams_to_create": [
                {"team_number": 1, "school_name": "Example University", "contact_email": "coach@example.edu"}
            ]
        }
    }
    session.save()

    response = _confirm(client)

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

    response = _confirm(client)

    assert b"Scores from the last event are still in the portal" in response.content
    assert not SchoolInfo.objects.exists()


def test_the_second_of_two_confirms_is_refused_instead_of_closing_the_first(gold_team_user):
    from registration.models import Event

    Team.objects.create(team_number=1, team_name="Team 01", is_active=True)
    first, second = _season_and_event(), _season_and_event(name="Other")
    clients = []
    for event in (first, second):
        staff = Client()
        staff.force_login(gold_team_user)
        csv = SimpleUploadedFile("schools.csv", b"school_name,contact_email\nExample University,coach@example.edu\n")
        staff.post(reverse("school_info_import"), {"upload": "true", "csv_file": csv, "event": str(event.pk)})
        clients.append(staff)

    _confirm(clients[0])
    response = _confirm(clients[1])

    assert b"Invitational (2026) is still open" in response.content
    assert list(Event.objects.filter(is_active=True)) == [first]


def test_reimporting_the_open_event_is_refused_once_its_packets_were_sent(client):
    from packets.models import Packet

    Team.objects.create(team_number=1, team_name="Team 01", is_active=True)
    event = _season_and_event(active=True)
    Packet.objects.create(
        event=event,
        title="Packet",
        file_data=b"x",
        filename="p.pdf",
        mime_type="application/pdf",
        file_size=1,
        status="completed",
        uploaded_by="gold",
    )
    csv = SimpleUploadedFile("schools.csv", b"school_name,contact_email\nExample University,coach@example.edu\n")

    response = client.post(reverse("school_info_import"), {"upload": "true", "csv_file": csv, "event": str(event.pk)})

    assert (
        b"Packets have already been sent for Invitational (2026). Re-importing reassigns team numbers, so sent "
        b"credentials would reach the wrong school. Change a single school on its School Info page instead."
    ) in response.content
    assert not SchoolInfo.objects.exists()


def test_a_draft_packet_does_not_block_reimporting_the_open_event(client):
    from packets.models import Packet

    Team.objects.create(team_number=1, team_name="Team 01", is_active=True)
    event = _season_and_event(active=True)
    Packet.objects.create(
        event=event,
        title="Packet",
        file_data=b"x",
        filename="p.pdf",
        mime_type="application/pdf",
        file_size=1,
        status="draft",
        uploaded_by="gold",
    )
    csv = SimpleUploadedFile("schools.csv", b"school_name,contact_email\nExample University,coach@example.edu\n")
    client.post(reverse("school_info_import"), {"upload": "true", "csv_file": csv, "event": str(event.pk)})

    _confirm(client)

    assert SchoolInfo.objects.exists()


def test_a_new_event_from_the_import_is_closed_to_registration(client):
    from registration.models import Event

    Team.objects.create(team_number=1, team_name="Team 01", is_active=True)
    _season_and_event()
    csv = SimpleUploadedFile("schools.csv", b"school_name,contact_email\nExample University,coach@example.edu\n")
    client.post(
        reverse("school_info_import"),
        {
            "upload": "true",
            "csv_file": csv,
            "event": "new",
            "new_name": "Qualifier",
            "new_date": "2026-11-07",
            "new_type": "qualifier",
        },
    )

    _confirm(client)

    new = Event.objects.get(name="Qualifier")
    assert new.is_active
    assert not new.registration_open


def test_reimporting_the_open_event_is_refused_while_its_scores_are_live(client):
    from scoring.models import ServiceScore

    team = Team.objects.create(team_number=1, team_name="Team 01", is_active=True)
    event = _season_and_event(active=True)
    ServiceScore.objects.create(team=team, service_points=5)
    csv = SimpleUploadedFile("schools.csv", b"school_name,contact_email\nExample University,coach@example.edu\n")

    response = client.post(reverse("school_info_import"), {"upload": "true", "csv_file": csv, "event": str(event.pk)})

    assert (
        f"Teams have already started using their team numbers for {event} (scores, tickets or Discord links). "
        "Re-importing reassigns team numbers under them. Change a single school on its School Info page instead."
    ).encode() in response.content
    assert not SchoolInfo.objects.exists()


def test_confirming_a_reimport_of_the_open_event_is_refused_once_scores_appear(client):
    from scoring.models import ServiceScore

    team = Team.objects.create(team_number=1, team_name="Team 01", is_active=True)
    event = _season_and_event(active=True)
    csv = SimpleUploadedFile("schools.csv", b"school_name,contact_email\nExample University,coach@example.edu\n")
    client.post(reverse("school_info_import"), {"upload": "true", "csv_file": csv, "event": str(event.pk)})
    ServiceScore.objects.create(team=team, service_points=5)

    response = _confirm(client)

    assert f"Teams have already started using their team numbers for {event}".encode() in response.content
    assert not SchoolInfo.objects.exists()


def test_the_session_keeps_the_raw_event_fields(client):
    Team.objects.create(team_number=1, team_name="Team 01", is_active=True)
    _season_and_event()
    csv = SimpleUploadedFile("schools.csv", b"school_name,contact_email\nExample University,coach@example.edu\n")
    fields = {"event": "new", "new_name": "Invitational 2", "new_date": "2026-11-07", "new_type": "invitational"}

    client.post(reverse("school_info_import"), {"upload": "true", "csv_file": csv, **fields})

    (stored,) = client.session["school_list_previews"].values()
    assert stored["event"] == {**fields, "mode": ""}


def test_confirm_rechecks_a_new_events_fields(client):
    from registration.models import Event

    Team.objects.create(team_number=1, team_name="Team 01", is_active=True)
    _season_and_event()
    csv = SimpleUploadedFile("schools.csv", b"school_name,contact_email\nExample University,coach@example.edu\n")
    fields = {"event": "new", "new_name": "Invitational 2", "new_date": "2026-11-07", "new_type": "invitational"}
    client.post(reverse("school_info_import"), {"upload": "true", "csv_file": csv, **fields})
    session = client.session
    (stored,) = session["school_list_previews"].values()
    stored["event"]["new_type"] = "bogus"
    session.save()

    response = _confirm(client)

    assert b"Upload the file again" in response.content
    assert not Event.objects.filter(name="Invitational 2").exists()
    assert not SchoolInfo.objects.exists()
