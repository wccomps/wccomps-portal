"""The import's Add mode: late schools join the open event on free teams; the list's schools keep their teams."""

from datetime import date

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse
from registration.models import Event, Season
from registration.services import set_team_school

from team.models import SchoolInfo, Team

pytestmark = pytest.mark.django_db

ADD_NEEDS_OPEN = "Add schools needs the open event. Use Replace for an event&#x27;s first school list."


@pytest.fixture
def client(gold_team_user):
    client = Client()
    client.force_login(gold_team_user)
    return client


@pytest.fixture
def event():
    season = Season.objects.create(year=2026, name="2026", is_active=True)
    return Event.objects.create(
        season=season, name="Invitational", event_type="invitational", date=date(2026, 10, 3), is_active=True
    )


@pytest.fixture
def teams():
    return [Team.objects.create(team_number=n, team_name=f"Team {n:02d}", is_active=False) for n in range(1, 5)]


def _csv(*names):
    lines = "".join(f"{name},coach{i}@example.edu\n" for i, name in enumerate(names))
    return SimpleUploadedFile("schools.csv", f"school_name,contact_email\n{lines}".encode())


def _upload(client, event_id, *names, mode="add"):
    return client.post(
        reverse("school_info_import"),
        {"upload": "true", "csv_file": _csv(*names), "event": str(event_id), "mode": mode},
    )


def _holders():
    return dict(SchoolInfo.objects.values_list("team__team_number", "school__name"))


def test_add_keeps_the_list_and_gives_new_schools_free_teams(client, event, teams):
    set_team_school(teams[0], "School One", contact_email="c@example.edu")
    set_team_school(teams[2], "School Three", contact_email="c@example.edu")

    preview = _upload(client, event.pk, "School One", "School Two").content.decode()
    response = client.post(reverse("school_info_import"), {"confirm": "true"})

    assert "Already on the list, skipped: School One" in preview
    assert "Adds 1 school(s) to Invitational (2026); the 2 school(s) already on the list keep their team numbers." in (
        preview
    )
    assert "Add schools to Invitational (2026)" in preview
    assert "Added 1 school(s) to Invitational (2026)." in response.content.decode()
    holders = _holders()
    assert {holders[1], holders[3]} == {"School One", "School Three"}
    new_team = next(number for number, name in holders.items() if name == "School Two")
    assert new_team in (2, 4)
    assert Team.objects.get(team_number=new_team).is_active
    assert event.team_assignments.get(team__team_number=new_team).school.name == "School Two"


def test_add_takes_the_lowest_free_teams(client, event, teams):
    set_team_school(teams[0], "School One", contact_email="c@example.edu")

    _upload(client, event.pk, "School Two", "School Three")
    client.post(reverse("school_info_import"), {"confirm": "true"})

    assert set(_holders()) == {1, 2, 3}


def test_add_works_after_a_packet_went_out(client, event, teams):
    from packets.models import Packet

    set_team_school(teams[0], "School One", contact_email="c@example.edu")
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

    _upload(client, event.pk, "School Two")
    client.post(reverse("school_info_import"), {"confirm": "true"})

    assert _holders() == {1: "School One", 2: "School Two"}


def test_add_with_too_few_free_teams_is_an_error(client, event, teams):
    set_team_school(teams[0], "School One", contact_email="c@example.edu")

    content = _upload(client, event.pk, "A School", "B School", "C School", "D School").content.decode()

    assert "Adding 4 school(s) needs 4 free team(s), but only 3 are free." in content
    assert "csv_import_data" not in client.session


def test_add_with_only_schools_already_on_the_list_is_an_error(client, event, teams):
    set_team_school(teams[0], "School One", contact_email="c@example.edu")

    content = _upload(client, event.pk, "School One").content.decode()

    assert "Every school in the file is already on the list." in content


def test_add_repeating_a_school_is_an_error(client, event, teams):
    content = _upload(client, event.pk, "School Two", "school two").content.decode()

    assert "Each school can appear only once" in content


def test_add_needs_the_open_event(client, event, teams):
    other = Event.objects.create(season=event.season, name="Next", event_type="invitational", date=date(2026, 11, 7))

    assert ADD_NEEDS_OPEN in _upload(client, other.pk, "School Two").content.decode()
    event.is_active = False
    event.save()
    assert ADD_NEEDS_OPEN in _upload(client, event.pk, "School Two").content.decode()
    new = client.post(
        reverse("school_info_import"),
        {
            "upload": "true",
            "csv_file": _csv("School Two"),
            "event": "new",
            "new_name": "Qualifier",
            "new_date": "2026-11-07",
            "new_type": "qualifier",
            "mode": "add",
        },
    )
    assert ADD_NEEDS_OPEN in new.content.decode()
    assert not SchoolInfo.objects.exists()


def test_a_team_given_a_school_between_preview_and_confirm_refuses_the_add(client, event, teams):
    _upload(client, event.pk, "School Two", "School Three")
    taken = Team.objects.get(team_number=client.session["csv_import_data"]["teams_to_create"][1]["team_number"])
    set_team_school(taken, "School One", contact_email="c@example.edu")

    response = client.post(reverse("school_info_import"), {"confirm": "true"})

    assert f"Team {taken.team_number} was given a school since the preview" in response.content.decode()
    assert _holders() == {taken.team_number: "School One"}


def test_an_unknown_mode_is_refused(client, event, teams):
    content = _upload(client, event.pk, "School Two", mode="merge").content.decode()

    assert "Pick Replace or Add." in content
    assert not SchoolInfo.objects.exists()


def test_the_upload_form_offers_both_modes(client, event):
    content = client.get(reverse("school_info_import")).content.decode()

    assert 'name="mode" value="replace"' in content
    assert 'name="mode" value="add"' in content


def test_a_replace_preview_says_so(client, event, teams):
    content = _upload(client, event.pk, "School Two", mode="replace").content.decode()

    assert "Replace the school list" in content
    assert "Mode:" in content
