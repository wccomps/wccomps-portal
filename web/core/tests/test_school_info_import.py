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


def _shown():
    """The active season's id as the import page posts it, or '' when none is active."""
    from registration.models import Season

    active = Season.objects.active()
    return str(active.pk) if active else ""


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
    fields = {
        "event": "new",
        "new_name": "Invitational 2",
        "new_date": "2026-11-07",
        "new_type": "invitational",
        "current_season": _shown(),
    }

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

    assert b"closed out or removed" in response.content
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
    assert b"closed out or removed" in response.content


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

    assert b"not recorded at any event yet" in response.content
    assert not SchoolInfo.objects.exists()


def test_confirm_is_refused_when_scores_appeared_since_the_preview(client):
    from scoring.models import ServiceScore

    team = Team.objects.create(team_number=1, team_name="Team 01", is_active=True)
    event = _season_and_event()
    csv = SimpleUploadedFile("schools.csv", b"school_name,contact_email\nExample University,coach@example.edu\n")
    client.post(reverse("school_info_import"), {"upload": "true", "csv_file": csv, "event": str(event.pk)})
    ServiceScore.objects.create(team=team, service_points=5)

    response = _confirm(client)

    assert b"not recorded at any event yet" in response.content
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
        b"credentials would reach the wrong school. Change a single school on its School Info page instead. "
        b"To add late schools, use Add schools."
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
            "current_season": _shown(),
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
        "Re-importing reassigns team numbers under them. Change a single school on its School Info page instead. "
        "To add late schools, use Add schools."
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
    fields = {
        "event": "new",
        "new_name": "Invitational 2",
        "new_date": "2026-11-07",
        "new_type": "invitational",
        "current_season": _shown(),
    }

    client.post(reverse("school_info_import"), {"upload": "true", "csv_file": csv, **fields})

    (stored,) = client.session["school_list_previews"].values()
    assert stored["event"] == {
        **fields,
        "mode": "",
        "new_season_name": "",
        "new_season_year": "",
    }


def test_confirm_rechecks_a_new_events_fields(client):
    from registration.models import Event

    Team.objects.create(team_number=1, team_name="Team 01", is_active=True)
    _season_and_event()
    csv = SimpleUploadedFile("schools.csv", b"school_name,contact_email\nExample University,coach@example.edu\n")
    fields = {
        "event": "new",
        "new_name": "Invitational 2",
        "new_date": "2026-11-07",
        "new_type": "invitational",
        "current_season": _shown(),
    }
    client.post(reverse("school_info_import"), {"upload": "true", "csv_file": csv, **fields})
    session = client.session
    (stored,) = session["school_list_previews"].values()
    stored["event"]["new_type"] = "bogus"
    session.save()

    response = _confirm(client)

    assert b"Upload the file again" in response.content
    assert not Event.objects.filter(name="Invitational 2").exists()
    assert not SchoolInfo.objects.exists()


def test_page_error_shows_once_not_under_each_form_field(client):
    Team.objects.create(team_number=1, team_name="Team 01", is_active=True)
    _season_and_event(name="Open", active=True)
    other = _season_and_event(name="Next")

    response = _upload(client, event=str(other.pk))

    assert response.content.decode().count("is still open") == 1


SEASON_FIELDS = {
    "event": "new",
    "new_name": "Invitational 1",
    "new_date": "2026-10-03",
    "new_type": "invitational",
    "new_season_name": "2026-2027",
    "new_season_year": "2027",
}


def _upload(client, **fields):
    csv = SimpleUploadedFile("schools.csv", b"school_name,contact_email\nExample University,coach@example.edu\n")
    return client.post(reverse("school_info_import"), {"upload": "true", "csv_file": csv, **fields})


def test_with_no_active_season_the_page_offers_a_new_one(client):
    page = client.get(reverse("school_info_import")).content.decode()

    assert 'name="new_season_name"' in page
    assert 'name="new_season_year"' in page


def test_with_an_active_season_the_page_names_it_and_still_offers_a_new_one(client):
    event = _season_and_event()

    page = client.get(reverse("school_info_import")).content.decode()

    assert "Unless you start a new season below, this event will be in season" in page
    assert "<strong>2026</strong>" in page
    assert 'name="new_season_name"' in page
    assert f'name="current_season" value="{event.season.pk}"' in page


def test_with_an_active_season_starting_a_new_one_puts_the_event_in_it(client):
    from registration.models import Event, Season

    old = _season_and_event().season
    Team.objects.create(team_number=1, team_name="Team 01", is_active=True)

    preview = _upload(client, **SEASON_FIELDS, current_season=str(old.pk))
    assert b"new season 2026-2027" in preview.content
    response = _confirm(client)

    season = Season.objects.get(year=2027)
    assert list(Season.objects.filter(is_active=True)) == [season]
    assert Event.objects.open().season == season
    assert b"Created the 2026-2027 season" in response.content


def test_with_an_active_season_a_new_event_without_a_new_season_goes_in_it(client):
    from registration.models import Event, Season

    old = _season_and_event().season
    Team.objects.create(team_number=1, team_name="Team 01", is_active=True)
    fields = {k: v for k, v in SEASON_FIELDS.items() if not k.startswith("new_season")}

    _upload(client, **fields, current_season=str(old.pk))
    _confirm(client)

    assert Event.objects.open().season == old
    assert list(Season.objects.all()) == [old]


def test_a_new_season_needs_a_new_event(client):
    event = _season_and_event()
    Team.objects.create(team_number=1, team_name="Team 01", is_active=True)

    response = _upload(
        client,
        event=str(event.pk),
        new_season_name="2026-2027",
        new_season_year="2027",
        current_season=str(event.season.pk),
    )

    assert b"A new season needs a new event" in response.content
    assert not SchoolInfo.objects.exists()


def test_a_season_activated_since_the_page_loaded_refuses_the_new_season(client):
    from registration.models import Season

    old = _season_and_event().season
    Team.objects.create(team_number=1, team_name="Team 01", is_active=True)
    _upload(client, **SEASON_FIELDS, current_season=str(old.pk))
    Season.objects.create(name="Other", year=2030).activate()

    response = _confirm(client)

    assert b"The active season changed since this page loaded" in response.content
    assert not Season.objects.filter(year=2027).exists()


def test_with_no_active_season_the_import_creates_it_with_the_event(client):
    from registration.models import Event, Season

    Season.objects.create(name="2025", year=2025, is_active=False)
    Team.objects.create(team_number=1, team_name="Team 01", is_active=True)

    preview = _upload(client, **SEASON_FIELDS)
    assert b"new season 2026-2027" in preview.content
    assert not Season.objects.filter(year=2027).exists()
    response = _confirm(client)

    season = Season.objects.get(year=2027)
    assert (season.name, season.is_active) == ("2026-2027", True)
    assert list(Season.objects.filter(is_active=True)) == [season]
    assert Event.objects.open().season == season
    assert SchoolInfo.objects.get().school.name == "Example University"
    assert b"Created the 2026-2027 season" in response.content


def test_with_no_active_season_the_season_fields_are_required(client):
    Team.objects.create(team_number=1, team_name="Team 01", is_active=True)

    response = _upload(client, **{**SEASON_FIELDS, "new_season_name": ""})

    assert b"Give the new season a name and a year." in response.content
    assert not SchoolInfo.objects.exists()


def test_with_no_active_season_a_taken_year_is_refused(client):
    from registration.models import Season

    Season.objects.create(name="Old", year=2027, is_active=False)
    Team.objects.create(team_number=1, team_name="Team 01", is_active=True)

    response = _upload(client, **SEASON_FIELDS)

    assert b"The Old season already has the year 2027" in response.content
    assert Season.objects.count() == 1


def test_a_season_activated_since_the_preview_refuses_the_import(client):
    from registration.models import Season

    Team.objects.create(team_number=1, team_name="Team 01", is_active=True)
    _upload(client, **SEASON_FIELDS)
    Season.objects.create(name="Other", year=2030, is_active=True)

    response = _confirm(client)

    assert b"Upload the file again" in response.content
    assert not Season.objects.filter(year=2027).exists()
    assert not SchoolInfo.objects.exists()


def _field_error(html: str, field: str) -> str | None:
    """The error shown in a field's own row, under the id its widget's aria-describedby names."""
    import re
    from html import unescape

    match = re.search(rf'<p class="errornote" id="id_{field}_error">([^<]*)</p>', html)
    return unescape(match.group(1)) if match else None


def test_a_refused_upload_shows_the_posted_values_and_the_error_beside_its_field(client):
    import re

    event = _season_and_event()
    Team.objects.create(team_number=1, team_name="Team 01", is_active=True)

    response = _upload(
        client,
        event="new",
        mode="add",
        new_name="Qualifier 9",
        new_date="not a date",
        new_type="qualifier",
        new_season_name="2026-2027",
        current_season=str(event.season.pk),
    )
    html = response.content.decode()

    assert not SchoolInfo.objects.exists()
    assert _field_error(html, "new_date") == "Give the new event a valid date."
    assert _field_error(html, "new_name") is None
    assert 'value="Qualifier 9"' in html
    assert 'value="2026-2027"' in html
    assert re.search(r'<option value="new" selected>New event</option>', html)
    assert re.search(r'<option value="qualifier" selected>', html)
    assert re.search(r'name="mode" value="add"[^>]* checked', html)


def test_an_upload_without_an_event_says_so_beside_the_event(client):
    _season_and_event()

    html = _upload(client, event="").content.decode()

    assert _field_error(html, "event") == "Pick the event this school list is for."
    assert not SchoolInfo.objects.exists()


def test_a_refused_upload_asks_for_the_file_again(client):
    """A browser can't put a file back in its input, so a page shown again after a refusal says to pick it again."""
    csv = SimpleUploadedFile("schools.csv", b"school_name,contact_email\nExample University,coach@example.edu\n")
    response = client.post(reverse("school_info_import"), {"upload": "1", "csv_file": csv, "event": ""})
    assert "Pick the file again" in response.content.decode()


def test_a_new_event_with_a_bad_season_says_why(client):
    _season_and_event(name="Earlier")
    csv = SimpleUploadedFile("schools.csv", b"school_name,contact_email\nExample University,coach@example.edu\n")
    fields = {"event": "new", "new_name": "Inv", "new_date": "2026-11-07", "new_type": "invitational"}
    response = client.post(
        reverse("school_info_import"), {"upload": "true", "csv_file": csv, **fields, "current_season": "x"}
    )
    assert "Current season: Enter a whole number." in response.content.decode()
