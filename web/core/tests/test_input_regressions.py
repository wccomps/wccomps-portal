"""Inputs the property tests found breaking the portal, each pinned with @example of its minimal failing input."""

import csv
import io
from datetime import date, timedelta

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse
from django.utils.http import content_disposition_header
from hypothesis import example, given
from hypothesis import strategies as st
from packets.forms import PacketResendForm
from packets.models import SENT_STATUSES, Packet
from packets.views import _parse_team_extras_csv
from registration.forms import RegistrationForm
from registration.models import SCHOOL_NAME_MAX_LENGTH, Event, Season, TeamRegistration, normalize_school_name
from registration.services import SchoolListRow, import_school_list
from scoring.screenshots import detect_inline_type

from core.admin_views.competition import TIMEZONE_CHOICES
from core.forms import SchoolInfoEditForm
from core.tests.strategies import (
    content_types,
    datetime_text,
    email_text,
    file_bytes,
    file_names,
    long_email,
    request_text,
    rolled_back,
)
from core.utils import parse_datetime_to_utc
from team.forms import parse_csv_file
from team.models import Team
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


# The 254-character column every stored email goes in (models.EmailField); a form's EmailField accepts up to 320
EMAIL_COLUMN = 254
STORED_EMAILS = (
    (SchoolInfoEditForm, ("contact_email", "secondary_email")),
    (PacketResendForm, ("primary_email", "secondary_email")),
    (RegistrationForm, ("captain_email", "coach_email")),
)


@given(email=email_text())
@example(email=long_email(EMAIL_COLUMN + 1))
def test_an_email_a_form_stores_fits_its_column(email: str) -> None:
    for form_class, names in STORED_EMAILS:
        for name in names:
            try:
                cleaned = form_class.base_fields[name].clean(email)
            except ValidationError:
                continue
            assert len(cleaned) <= EMAIL_COLUMN, (
                f"{form_class.__name__}.{name} accepts a {len(cleaned)}-character email"
            )


@given(name=request_text(SCHOOL_NAME_MAX_LENGTH))
@example(name="ß" * 200)
def test_a_school_name_a_form_accepts_has_a_key_that_fits(name: str) -> None:
    """School names are matched on their case-folded key, which can be longer than the name. The registration
    form checks its name with the model field's validators (ModelForm's full_clean)."""
    registration_field = TeamRegistration._meta.get_field("school_name")
    for clean in (SchoolInfoEditForm.base_fields["school_name"].clean, lambda n: registration_field.clean(n, None)):
        try:
            cleaned = clean(name)
        except ValidationError:
            continue
        assert len(normalize_school_name(cleaned)) <= SCHOOL_NAME_MAX_LENGTH


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
    assert not any(e.startswith("Error parsing CSV file") for e in parsed["errors"]), parsed["errors"]
    if content.startswith(b"\xef\xbb\xbfschool_name,contact_email\r\n"):
        assert parsed["warnings"] == []


@given(text=request_text())
@example(text='team\r""\r')
@example(text="team,key\n" + "9" * 5000 + ",x\n")
@example(text="team,key\n1\n")
def test_a_packets_per_team_csv_parses_or_gives_nothing(text: str) -> None:
    """The view says the per-team data can't be parsed when the parser gives nothing; it must not raise."""
    for team, values in _parse_team_extras_csv(text).items():
        assert team == str(int(team))
        assert all(isinstance(v, str) for v in values.values())


@pytest.mark.django_db
@given(name=file_names(), content_type=content_types())
@example(name="a.pdf", content_type="application/" + "x" * 100)
@example(name='a"b.pdf', content_type="application/pdf")
def test_an_uploaded_packet_is_stored_and_downloads(name: str, content_type: str, gold_team_user: User) -> None:
    """The client's file name and content type are stored within their columns and sent back as safe headers."""
    client = Client()
    client.force_login(gold_team_user)
    upload = SimpleUploadedFile(name or "unnamed", b"%PDF-1.7 packet", content_type=content_type)
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
    upload = SimpleUploadedFile(name or "unnamed", content, content_type=content_type)
    with rolled_back():
        team = Team.objects.create(team_number=1, team_name="Team 01")
        ticket = Ticket.objects.create(team=team, category=TicketCategory.objects.first(), ticket_number="T001-001")
        response = client.post(reverse("ticket_attachment_upload", args=[ticket.ticket_number]), {"attachment": upload})
        assert response.status_code < 500
        for attachment in TicketAttachment.objects.filter(ticket=ticket):
            url = reverse("ticket_attachment_download", args=[ticket.ticket_number, attachment.pk]) + "?inline=1"
            download = client.get(url)
            inline_type = detect_inline_type(content)
            assert download["Content-Type"] == (inline_type or attachment.mime_type)
            assert download["Content-Disposition"].startswith("inline" if inline_type else "attachment")
