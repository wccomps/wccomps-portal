"""Inputs the property tests found breaking the portal outside its forms, each pinned with @example of a failing
input. A form's failing POSTs are pinned in its FormCase examples (test_properties_forms.py)."""

import csv
import io
import unicodedata
from datetime import date, timedelta

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import Client
from django.urls import reverse
from django.utils.http import content_disposition_header
from hypothesis import example, given
from hypothesis import strategies as st
from packets.forms import PacketResendForm, parse_team_extras
from packets.models import SENT_STATUSES, Packet
from registration.forms import EventForm, RegistrationForm
from registration.models import (
    SCHOOL_NAME_MAX_LENGTH,
    Event,
    RegistrationContact,
    School,
    Season,
    normalize_school_name,
)
from registration.services import SchoolListRow, import_school_list
from scoring.models import ScoringTemplate
from scoring.screenshots import read_screenshot

from core.admin_views.competition import TIMEZONE_CHOICES
from core.forms import SchoolInfoEditForm
from core.stored_files import DOWNLOAD_MIME, detect_inline_type
from core.tests.strategies import (
    content_types,
    datetime_text,
    email_text,
    file_bytes,
    file_names,
    request_text,
    rolled_back,
    uploaded_file,
)
from core.utils import parse_datetime_to_utc
from team.forms import parse_csv_file
from team.models import SchoolInfo, Team
from ticketing.models import Ticket, TicketAttachment, TicketCategory


@given(when=datetime_text(), tz=st.one_of(st.sampled_from([tz for tz, _ in TIMEZONE_CHOICES]), request_text(40)))
@example(when="0001-01-01T00:00", tz="Asia/Tokyo")
@example(when="9999-12-31T23:59", tz="America/Los_Angeles")
def test_schedule_time_parses_to_utc_or_raises_value_error(when: str, tz: str) -> None:
    """A schedule time at either end of the calendar, shifted past it by its timezone, is refused, not a 500."""
    try:
        result = parse_datetime_to_utc(when, tz)
    except ValueError:
        return
    assert result.utcoffset() == timedelta(0)


# Each form email field with the column it is saved in
STORED_EMAILS = (
    (SchoolInfoEditForm, "contact_email", SchoolInfo, "contact_email"),
    (SchoolInfoEditForm, "secondary_email", SchoolInfo, "secondary_email"),
    (PacketResendForm, "primary_email", SchoolInfo, "contact_email"),
    (PacketResendForm, "secondary_email", SchoolInfo, "secondary_email"),
    (RegistrationForm, "captain_email", RegistrationContact, "email"),
    (RegistrationForm, "coach_email", RegistrationContact, "email"),
)


@given(email=email_text())
def test_an_email_a_form_stores_fits_its_column(email: str) -> None:
    """A form EmailField alone accepts 320 characters; the columns hold 254."""
    for form_class, name, model, column_name in STORED_EMAILS:
        column_length = model._meta.get_field(column_name).max_length
        assert form_class.base_fields[name].max_length == column_length
        try:
            cleaned = form_class.base_fields[name].clean(email)
        except ValidationError:
            continue
        assert len(cleaned) <= column_length, f"{form_class.__name__}.{name} accepts a {len(cleaned)}-character email"


@pytest.mark.django_db
@given(
    name=request_text(SCHOOL_NAME_MAX_LENGTH).filter(
        lambda n: n.strip() and len(n) <= SCHOOL_NAME_MAX_LENGTH and "\x00" not in n
    )
)
@example(name="ß" * 255)
def test_school_for_name_saves_any_name_the_forms_take(name: str) -> None:
    """Schools are matched on the case-folded name, which can be longer than the name (ß is ss). for_name is also
    where an older registration's name becomes a school on approval, without any form."""
    with rolled_back():
        assert School.for_name(name).name_key == normalize_school_name(name)


@pytest.fixture
def school_list_event(db: object) -> Event:
    season = Season.objects.create(name="2026", year=2026, is_active=True)
    Team.objects.create(team_number=1, team_name="Team 01")
    return Event.objects.create(season=season, name="Invitational", event_type="invitational", date=date(2026, 10, 3))


def _school_csv(*rows: list[str], header: str = "school_name,contact_email,notes", bom: bool = False) -> bytes:
    out = io.StringIO()
    out.write(header + "\r\n")
    csv.writer(out).writerows(rows)
    return (b"\xef\xbb\xbf" if bom else b"") + out.getvalue().encode("utf-8")


@pytest.mark.django_db
@given(name=request_text(SCHOOL_NAME_MAX_LENGTH), notes=request_text(300))
@example(name="a\x00b", notes="")
@example(name="y" * (SCHOOL_NAME_MAX_LENGTH + 1), notes="")
@example(name="ß" * 200, notes="")
@example(name="School", notes="\x00")
def test_a_school_list_row_the_csv_parser_accepts_imports(name: str, notes: str, school_list_event: Event) -> None:
    """The parser checks a row as the school info form does, so what it accepts saves without a database error."""
    parsed = parse_csv_file(SimpleUploadedFile("s.csv", _school_csv([name, "coach@example.edu", notes])))
    if parsed["errors"]:
        assert all(e.startswith("Row 2: ") for e in parsed["errors"]), parsed["errors"]
        return
    row = parsed["rows"][0]
    with rolled_back():
        import_school_list(
            school_list_event,
            [SchoolListRow(1, row["school_name"], row["contact_email"], row["secondary_email"], row["notes"])],
            "gold",
        )


@example(content=_school_csv(["School"], header="school_name,contact_email"))
@example(content=_school_csv(["School", "coach@example.edu"], header="school_name,contact_email", bom=True))
@given(content=st.binary(max_size=300))
def test_a_school_list_file_gets_row_errors_not_a_parse_failure(content: bytes) -> None:
    """A short row is a missing value in that row, and a byte order mark is not part of the first column's name."""
    parsed = parse_csv_file(SimpleUploadedFile("s.csv", content))
    assert all(isinstance(e, str) and e for e in parsed["errors"] + parsed["warnings"])
    if content == _school_csv(["School"], header="school_name,contact_email"):
        assert parsed["errors"] == ["Row 2: contact_email: This field is required."]
    if content.startswith(b"\xef\xbb\xbfschool_name,contact_email\r\n"):
        assert parsed["warnings"] == []
        assert parsed["rows"][0]["school_name"] == "School"


@given(text=request_text())
@example(text='team\r""\r')
@example(text="team,key\r1,x\r")
@example(text="team,key\n" + "9" * 5000 + ",x\n")
@example(text="team,key\n1\n")
def test_a_packets_per_team_csv_parses_or_says_why_not(text: str) -> None:
    """A carriage return alone ends a line, as in any CSV; text with no team rows is refused with a reason."""
    try:
        result = parse_team_extras(text)
    except ValidationError as refused:
        assert refused.messages
        return
    for team, values in result.items():
        assert team == str(int(team))
        assert all(isinstance(v, str) for v in values.values())
    if text == "team,key\r1,x\r":
        assert result == {"1": {"key": "x"}}


@pytest.mark.django_db
@given(name=file_names(), content_type=content_types())
@example(name="a.pdf", content_type="application/" + "x" * 100)
@example(name='a"b.pdf', content_type="application/pdf")
def test_an_uploaded_packet_is_stored_and_downloads(name: str, content_type: str, gold_team_user: User) -> None:
    """The client's file name and content type are stored within their columns and sent back as safe headers."""
    client = Client()
    client.force_login(gold_team_user)
    upload = uploaded_file(b"%PDF-1.7 packet", name, content_type)
    with rolled_back():
        response = client.post(reverse("upload_packet"), {"title": "Packet", "packet_file": upload})
        assert response.status_code < 500
        packet = Packet.objects.order_by("-pk").first()
        if packet is None:
            return
        Packet.objects.filter(pk=packet.pk).update(status=SENT_STATUSES[0], web_access_enabled=True)
        download = client.get(reverse("download_packet", args=[packet.pk]))
        assert download.status_code == 200
        assert download["Content-Disposition"] == content_disposition_header(
            as_attachment=True, filename=packet.filename
        )


@pytest.mark.django_db
@given(name=file_names(), content_type=content_types(), content=file_bytes())
@example(name="a.png", content_type="image/" + "x" * 100, content=b"\x89PNG\r\n\x1a\n")
@example(name="a.png", content_type="image/png", content=b"<script>alert(1)</script>")
def test_a_ticket_attachment_is_typed_from_its_bytes(
    name: str, content_type: str, content: bytes, ticketing_support_user: User
) -> None:
    """A client's claimed type is neither stored (it can be longer than the column) nor trusted to show inline."""
    client = Client()
    client.force_login(ticketing_support_user)
    upload = uploaded_file(content, name, content_type)
    with rolled_back():
        team = Team.objects.create(team_number=1, team_name="Team 01")
        ticket = Ticket.objects.create(team=team, category=TicketCategory.objects.first(), ticket_number="T001-001")
        response = client.post(reverse("ticket_attachment_upload", args=[ticket.ticket_number]), {"attachment": upload})
        assert response.status_code < 500
        for attachment in TicketAttachment.objects.filter(ticket=ticket):
            url = reverse("ticket_attachment_download", args=[ticket.ticket_number, attachment.pk]) + "?inline=1"
            download = client.get(url)
            inline_type = detect_inline_type(content)
            assert download["Content-Type"] == (inline_type or DOWNLOAD_MIME)
            assert download["Content-Disposition"].startswith("inline" if inline_type else "attachment")


@pytest.mark.django_db
@given(name=st.text(min_size=1, max_size=100).filter(lambda s: s.strip() and "\x00" not in s))
@example(name="Category")
def test_a_new_ticket_category_gets_an_id_past_the_seeded_ones(name: str) -> None:
    """Migration 0009 seeds categories 1 to 6 with explicit ids, which doesn't move the id sequence on."""
    with rolled_back():
        assert TicketCategory.objects.create(display_name=name).pk > 6


INTEGER_COLUMN = connection.ops.integer_field_range("IntegerField")


@pytest.fixture
def scoring_template(db: object) -> ScoringTemplate:
    return ScoringTemplate.objects.first() or ScoringTemplate.objects.create()


@pytest.mark.django_db
@given(name=file_names())
@example(name="a\x00.png")
@example(name="a\x1f‮.png")
def test_an_upload_name_is_stored_without_control_or_bidi_characters(
    name: str, ticketing_support_user: User, red_team_user: User
) -> None:
    """A file name arrives from the client (RFC 2231 can encode a NUL); it is stored, so it must be storable."""
    client = Client()
    client.force_login(ticketing_support_user)
    upload = uploaded_file(b"\x89PNG\r\n\x1a\n", name, "image/png")
    with rolled_back():
        team = Team.objects.create(team_number=1, team_name="Team 01")
        ticket = Ticket.objects.create(team=team, category=TicketCategory.objects.first(), ticket_number="T001-001")
        response = client.post(reverse("ticket_attachment_upload", args=[ticket.ticket_number]), {"attachment": upload})
        assert response.status_code < 500
        for attachment in TicketAttachment.objects.filter(ticket=ticket):
            assert attachment.filename and not _disguising(attachment.filename)
    upload.seek(0)
    _, stored_name, _ = read_screenshot(upload)
    assert stored_name and not _disguising(stored_name)


def _disguising(name: str) -> bool:
    """A control character (a NUL can't be stored) or a bidi control (it reorders the shown name)."""
    return any(
        unicodedata.category(c) == "Cc" or c in "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069" for c in name
    )


@pytest.mark.django_db
def test_a_clashing_event_is_refused_with_the_constraints_message(school_list_event: Event) -> None:
    """The model's constraint decides a clash, with its own message, on the event number."""
    Event.objects.filter(pk=school_list_event.pk).update(event_type="qualifier", event_number=1)
    post = {"name": "Next", "event_type": "qualifier", "event_number": "1", "date": "2026-11-07"}
    post |= {"start_time": "09:00", "end_time": "17:00", "max_teams": "50"}
    form = EventForm(post, instance=Event(season=school_list_event.season))
    assert not form.is_valid()
    assert form.errors["event_number"] == ["This season already has an event of this type with this number."]
