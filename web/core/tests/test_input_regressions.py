"""Inputs the property tests found breaking the portal, each pinned with @example of its minimal failing input."""

import csv
import io
import unicodedata
from datetime import date, timedelta

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.http import QueryDict
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from django.utils.http import content_disposition_header
from hypothesis import example, given
from hypothesis import strategies as st
from orange_team.forms import FollowUpForm, OrangeCheckForm, extract_criteria
from orange_team.models import OrangeCheck
from orange_team.services import update_check_criteria
from packets.forms import PacketResendForm, parse_team_extras
from packets.models import SENT_STATUSES, Packet
from registration.forms import EventForm, RegistrationForm
from registration.models import SCHOOL_NAME_MAX_LENGTH, Event, School, Season, TeamRegistration, normalize_school_name
from registration.services import SchoolListRow, import_school_list
from scoring.models import ScoringTemplate
from scoring.screenshots import detect_inline_type, read_screenshot

from core.admin_views.competition import TIMEZONE_CHOICES
from core.forms import ResetPasswordsForm, SchoolInfoEditForm, StartMessageForm
from core.tests.strategies import (
    content_types,
    datetime_text,
    email_text,
    file_bytes,
    file_names,
    int_text,
    long_email,
    request_text,
    rolled_back,
    uploaded_file,
)
from core.utils import parse_datetime_to_utc
from team.forms import parse_csv_file
from team.models import Team
from ticketing.forms import TicketCategoryForm, TicketResolveForm
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


@pytest.mark.django_db
@given(name=request_text(255))
@example(name="ß" * 255)
def test_a_school_name_a_form_accepts_is_a_school_that_saves(name: str) -> None:
    """Schools are matched on the case-folded name, which can be longer than the name (ß is ss): the school
    info form and the registration (its model field's validation) accept any 255-character name, and the
    school saves. School.for_name is also where an older registration's name becomes a school on approval."""
    registration_field = TeamRegistration._meta.get_field("school_name")

    def registration_clean(value: str) -> object:
        cleaned = RegistrationForm.base_fields["school_name"].clean(value)
        registration_field.run_validators(cleaned)
        return cleaned

    for clean in (SchoolInfoEditForm.base_fields["school_name"].clean, registration_clean):
        try:
            cleaned = clean(name)
        except ValidationError:
            continue
        with rolled_back():
            assert School.for_name(cleaned).name_key == normalize_school_name(cleaned)


@pytest.mark.django_db
def test_a_school_name_of_255_letters_that_fold_longer_is_accepted() -> None:
    form = SchoolInfoEditForm({"school_name": "ß" * 255, "contact_email": "coach@example.edu"})
    assert form.is_valid(), form.errors


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
            assert download["Content-Type"] == (inline_type or attachment.mime_type)
            assert download["Content-Disposition"].startswith("inline" if inline_type else "attachment")


@pytest.mark.django_db
@given(name=st.text(min_size=1, max_size=100).filter(lambda s: s.strip() and "\x00" not in s))
@example(name="Category")
def test_a_new_ticket_category_gets_an_id_past_the_seeded_ones(name: str) -> None:
    """Migration 0009 seeds categories 1 to 6 with explicit ids, which doesn't move the id sequence on."""
    with rolled_back():
        assert TicketCategory.objects.create(display_name=name).pk > 6


INTEGER_COLUMN = connection.ops.integer_field_range("IntegerField")


@pytest.mark.django_db
@given(value=int_text())
@example(value=str(2**31))
@example(value=str(-(2**31) - 1))
def test_an_integer_a_form_stores_fits_its_column(value: str) -> None:
    """Category points and sort order, and a resolved ticket's points, are stored in integer columns."""
    low, high = INTEGER_COLUMN
    for name in ("points", "min_points", "max_points", "sort_order"):
        form = TicketCategoryForm({"display_name": "Category", "points": "0", name: value})
        if form.is_valid():
            assert low <= form.cleaned_data[name] <= high, f"TicketCategoryForm.{name} accepts {value}"
    form = TicketResolveForm({"points_override": value})
    if form.is_valid() and form.cleaned_data["points_override"] is not None:
        assert low <= form.cleaned_data["points_override"] <= high


@pytest.fixture
def scoring_template(db: object) -> ScoringTemplate:
    return ScoringTemplate.objects.first() or ScoringTemplate.objects.create()


@pytest.mark.django_db
@given(max_points=int_text(), minutes=int_text())
@example(max_points="100000000", minutes="")
@example(max_points="", minutes="1440000000000")
def test_an_orange_check_the_form_accepts_saves(
    max_points: str, minutes: str, scoring_template: ScoringTemplate
) -> None:
    """Saving a check recomputes ScoringTemplate.orange_max (numeric(10, 2)) from every check's points."""
    form = OrangeCheckForm({"title": "Check", "max_points": max_points, "time_limit_minutes": minutes})
    if not form.is_valid():
        return
    with rolled_back():
        OrangeCheck.objects.create(
            title="Check",
            description="",
            max_points=form.cleaned_data["max_points"] or 0,
            time_limit=timedelta(minutes=m) if (m := form.cleaned_data["time_limit_minutes"]) else None,
        )


@pytest.mark.django_db
@given(points=st.one_of(int_text(), st.sampled_from(("²", "³", "¹⁰", "٣"))), row_id=int_text())
@example(points="²", row_id="")
@example(points="1", row_id="²")
@example(points="99999999999", row_id="")
def test_a_criterion_row_extract_criteria_keeps_saves(
    points: str, row_id: str, scoring_template: ScoringTemplate
) -> None:
    post = QueryDict(mutable=True)
    post.update({"criterion_label_1": "Works", "criterion_points_1": points, "criterion_id_1": row_id})
    try:
        criteria = extract_criteria(post)
    except ValidationError:
        return  # the page shows the refusal and changes nothing
    with rolled_back():
        check = OrangeCheck.objects.create(title="Check", description="")
        update_check_criteria(check, criteria)


@pytest.mark.django_db
@given(minutes=int_text())
@example(minutes="-1065460825")
@example(minutes="10000000000")
def test_a_follow_up_the_form_accepts_has_a_storable_time(minutes: str) -> None:
    form = FollowUpForm({"assignment_id": "1", "minutes": minutes})
    if form.is_valid():
        remind_at = timezone.now() + timedelta(minutes=form.cleaned_data["minutes"])
        assert remind_at > timezone.now()


@pytest.mark.django_db
@given(event_type=st.sampled_from([t for t, _ in Event.EVENT_TYPE_CHOICES]), number=st.sampled_from(("", "1", "2")))
@example(event_type="qualifier", number="1")
def test_an_event_the_form_accepts_saves(event_type: str, number: str, school_list_event: Event) -> None:
    """Events are unique by season, type and number; the form doesn't have the season, the view sets it."""
    season = school_list_event.season
    Event.objects.filter(pk=school_list_event.pk).update(event_type="qualifier", event_number=1)
    post = {"name": "Next", "event_type": event_type, "event_number": number, "date": "2026-11-07"}
    post |= {"start_time": "09:00", "end_time": "17:00", "max_teams": "50"}
    form = EventForm(post, instance=Event(season=season))
    if form.is_valid():
        with rolled_back():
            form.save()


@given(message=request_text(StartMessageForm.MAX_LENGTH))
@example(message="0\r")
@example(message="a\r\r\nb")
def test_a_start_message_has_one_kind_of_line_end_and_cleans_the_same_again(message: str) -> None:
    """Browsers post a textarea's line ends as CRLF; the message is stored and counted with LF only."""
    form = StartMessageForm({"start_message": message})
    if not form.is_valid():
        return
    cleaned = form.cleaned_data["start_message"]
    assert "\r" not in cleaned
    again = StartMessageForm({"start_message": cleaned})
    assert again.is_valid() and again.cleaned_data["start_message"] == cleaned


@pytest.mark.django_db
@given(name=file_names())
@example(name="a\x00.png")
@example(name="a\x1f‮.png")
def test_an_upload_name_is_stored_as_text_without_control_characters(
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
            assert attachment.filename and not any(unicodedata.category(c) in ("Cc", "Cf") for c in attachment.filename)
    upload.seek(0)
    _, stored_name, _ = read_screenshot(upload)
    assert stored_name and not any(unicodedata.category(c) in ("Cc", "Cf") for c in stored_name)


@given(numbers=st.one_of(request_text(20), st.sampled_from((",", " , ", "1,", "1-3"))))
@example(numbers=",")
def test_reset_passwords_reads_no_team_numbers_as_every_team(numbers: str) -> None:
    """A list that names no team is None (every active team), the same as an empty field, not an empty list."""
    form = ResetPasswordsForm({"team_numbers": numbers})
    if form.is_valid():
        assert form.cleaned_data["team_numbers"] is None or form.cleaned_data["team_numbers"]
