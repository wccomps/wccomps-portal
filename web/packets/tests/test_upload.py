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


def test_a_refused_upload_without_a_file_asks_only_for_the_file(gold_client):
    html = gold_client.post(reverse("upload_packet"), {"title": "No file"}).content.decode()

    assert field_error(html, "id_packet_file") == "This field is required."
    assert 'value="No file"' in html
