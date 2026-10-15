"""Stored files (core.stored_files): what is stored for a client's file name and type, and how files are served."""

import unicodedata

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from hypothesis import example, given
from hypothesis import strategies as st
from packets.models import Packet
from scoring.models import IncidentScreenshot, RedTeamScreenshot

from core.stored_files import (
    DOWNLOAD_MIME,
    MAX_NAME_LENGTH,
    clean_name,
    detect_inline_type,
    file_response,
    stored_type,
)
from core.tests.strategies import assert_storable, column, file_bytes, file_names, request_text, rolled_back
from team.models import Team
from ticketing.models import Ticket, TicketAttachment, TicketCategory

BIDI_CONTROLS = "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
FILE_TABLES = (Packet, TicketAttachment, RedTeamScreenshot, IncidentScreenshot)


def test_every_stored_file_name_column_holds_the_cleaned_name_length() -> None:
    assert {column(model, "filename").max_length for model in FILE_TABLES} == {MAX_NAME_LENGTH}


@pytest.mark.django_db
@given(name=st.one_of(file_names(), request_text(300)), default=st.sampled_from(("", "unnamed")))
@example(name="a\x00b\u202egnp.exe", default="")
@example(name="می\u200cروم.png", default="")  # Persian, with a zero-width non-joiner
def test_a_cleaned_name_is_storable_and_only_loses_control_and_bidi_characters(name: str, default: str) -> None:
    cleaned = clean_name(name, default)
    assert len(cleaned) <= MAX_NAME_LENGTH
    assert not any(unicodedata.category(c) == "Cc" or c in BIDI_CONTROLS for c in cleaned)
    kept = "".join(c for c in name if unicodedata.category(c) != "Cc" and c not in BIDI_CONTROLS).strip()
    assert cleaned == (kept[:MAX_NAME_LENGTH] or default)
    for model in FILE_TABLES:
        assert_storable(column(model, "filename"), cleaned or "x")


@given(data=file_bytes(), name=file_names())
@example(data=b"\x89PNG\r\n\x1a\n", name="pasted")
@example(data=b"<html><script>alert(1)</script>", name="shot.png")
def test_the_stored_type_is_the_bytes_for_an_inline_type_and_the_name_otherwise(data: bytes, name: str) -> None:
    """An image pasted without an extension still stores as an image, so its ticket shows a preview."""
    kind = stored_type(data, name)
    assert len(kind) <= 100
    if detect_inline_type(data):
        assert kind == detect_inline_type(data)
    else:
        assert not kind.startswith("image/") and kind != "application/pdf"


@given(data=file_bytes(), name=file_names(), inline=st.booleans())
def test_a_served_file_shows_inline_only_as_an_image_or_pdf_and_is_sandboxed_otherwise(
    data: bytes, name: str, inline: bool
) -> None:
    response = file_response(data, clean_name(name, "file"), inline=inline)
    inline_type = detect_inline_type(data)
    shown = inline and inline_type is not None
    assert response["Content-Type"] == (inline_type or DOWNLOAD_MIME)
    assert response["Content-Disposition"].startswith("inline" if shown else "attachment")
    if not (shown and inline_type == "application/pdf"):
        assert response["Content-Security-Policy"].startswith("sandbox")


@pytest.mark.django_db
def test_a_ticket_attachment_downloads_sandboxed(ticketing_support_user: User) -> None:
    client = Client()
    client.force_login(ticketing_support_user)
    with rolled_back():
        team = Team.objects.create(team_number=1, team_name="Team 01")
        ticket = Ticket.objects.create(team=team, category=TicketCategory.objects.first(), ticket_number="T001-001")
        attachment = TicketAttachment.objects.create(
            ticket=ticket, file_data=b"<svg onload=alert(1)>", filename="a.svg", mime_type="image/svg+xml"
        )
        download = client.get(reverse("ticket_attachment_download", args=[ticket.ticket_number, attachment.pk]))
    assert download["Content-Disposition"].startswith("attachment")
    assert download["Content-Security-Policy"].startswith("sandbox")
