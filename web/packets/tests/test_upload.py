"""The packet upload page shows a refused upload again with each error beside its field."""

import re

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse

from core.tests.form_html import field_error
from packets.forms import TEAM_EXTRAS_HELP
from packets.models import Packet

pytestmark = pytest.mark.django_db


@pytest.fixture
def gold_client(gold_team_user):
    client = Client()
    client.force_login(gold_team_user)
    return client


def test_a_new_upload_sends_by_email_and_on_the_web_unless_unticked(gold_client):
    html = gold_client.get(reverse("upload_packet")).content.decode()

    assert re.search(r'<input type="checkbox" name="send_via_email"[^>]* checked', html)
    assert re.search(r'<input type="checkbox" name="web_access_enabled"[^>]* checked', html)


def test_a_refused_upload_shows_the_posted_values_and_the_error_beside_its_field(gold_client):
    response = gold_client.post(
        reverse("upload_packet"),
        {
            "title": "Qualifier packet",
            "notes": "Read the rules first",
            "team_extras": "school,api_key\nExample,sk-1",
            "web_access_enabled": "on",
            "packet_file": SimpleUploadedFile("packet.pdf", b"%PDF-1.7 packet"),
        },
    )
    html = response.content.decode()

    assert response.status_code == 200
    assert not Packet.objects.exists()
    assert field_error(html, "id_team_extras") == f"The per-team data has no 'team' column. {TEAM_EXTRAS_HELP}"
    assert field_error(html, "id_title") is None
    assert 'value="Qualifier packet"' in html
    assert "Read the rules first</textarea>" in html
    assert "school,api_key\nExample,sk-1</textarea>" in html
    assert not re.search(r'name="send_via_email"[^>]* checked', html)
    assert re.search(r'name="web_access_enabled"[^>]* checked', html)
    assert field_error(html, "id_packet_file") == "Pick the file again: a page shown again with errors can't keep it."


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_a_refused_upload_counts_physical_lines_in_the_original_paste(gold_client, newline: str) -> None:
    text = newline.join(["", " \t", "  team,key", "", 'Team01,"' + "x" * 140_000])
    response = gold_client.post(
        reverse("upload_packet"),
        {
            "title": "Qualifier packet",
            "team_extras": text,
            "packet_file": SimpleUploadedFile("packet.pdf", b"%PDF-1.7 packet"),
        },
    )

    assert response.status_code == 200
    assert not Packet.objects.exists()
    assert field_error(response.content.decode(), "id_team_extras") == (
        f"The per-team data isn't CSV the upload can read (line 5). {TEAM_EXTRAS_HELP}"
    )


def test_a_refused_upload_without_a_file_asks_only_for_the_file(gold_client):
    html = gold_client.post(reverse("upload_packet"), {"title": "No file"}).content.decode()

    assert field_error(html, "id_packet_file") == "This field is required."
    assert 'value="No file"' in html


@pytest.mark.parametrize("team_extras", ["", " \r\n\t\n "])
def test_an_accepted_upload_keeps_the_files_name_and_size(gold_client, team_extras: str) -> None:
    response = gold_client.post(
        reverse("upload_packet"),
        {
            "title": "Qualifier packet",
            "team_extras": team_extras,
            "packet_file": SimpleUploadedFile("qualifier rules.pdf", b"%PDF-1.7 packet"),
        },
    )

    assert response.status_code == 302
    packet = Packet.objects.get()
    assert (packet.filename, packet.file_size) == ("qualifier rules.pdf", 15)
    assert packet.team_extras == {}


def test_an_accepted_upload_stores_extras_only_for_the_teams_named_by_the_selected_cells(gold_client) -> None:
    response = gold_client.post(
        reverse("upload_packet"),
        {
            "title": "Qualifier packet",
            "team_extras": (
                "\n \r\n  team,key,team note,team,key\n"
                "Team01,ignore,Team02\n"
                "\n"
                "Team02,first,Team04,Team03, last ,unnamed surplus\n"
                "Team03,ignore,Team03,Team04\n"
            ),
            "packet_file": SimpleUploadedFile("packet.pdf", b"%PDF-1.7 packet"),
        },
    )

    assert response.status_code == 302
    assert Packet.objects.get().team_extras == {
        "3": {"key": "last", "team note": "Team04"},
        "4": {"key": "", "team note": "Team03"},
    }
