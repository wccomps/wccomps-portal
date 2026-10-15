"""Property tests for every function that parses an uploaded file or CSV text, and the views that take the uploads.

Whatever bytes arrive (random, another encoding, a BOM, CRLF, huge or ragged rows, quoted newlines, formula cells),
a parser answers with its result or its structured error, never an exception; what it stores fits its columns,
and what a page echoes back is escaped. Valid generated rows come back out as the values that went in.

PARSERS maps each parser test_property_coverage finds to the tests here that cover it.
"""

import csv
import io
import mimetypes
import unicodedata
from collections.abc import Callable
from datetime import date
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.validators import validate_email
from django.http import HttpResponse
from django.template.loader import render_to_string
from django.test import Client
from django.urls import reverse
from hypothesis import given
from hypothesis import strategies as st
from packets.forms import PacketUploadForm
from packets.models import Packet
from packets.views import _parse_team_extras_csv
from registration.models import Event, Season, normalize_school_name
from registration.services import ImportBlockedError, SchoolListRow, import_school_list
from scoring.models import IncidentScreenshot, RedTeamScreenshot, validate_file_size
from scoring.screenshots import (
    DOWNLOAD_MIME,
    MAX_SCREENSHOTS,
    ScreenshotError,
    detect_inline_type,
    read_screenshot,
    read_screenshots,
    screenshot_response,
)

from core.forms import SchoolInfoEditForm
from core.tests.strategies import (
    PAYLOADS,
    assert_storable,
    column,
    email_text,
    file_bytes,
    request_text,
    rolled_back,
    uploaded_files,
)
from core.utils import upload_name
from team.forms import parse_csv_file, validate_csv_data
from team.models import Team
from ticketing.models import Ticket, TicketAttachment, TicketCategory
from ticketing.views.attachments import _save_attachment

pytestmark = pytest.mark.django_db

# A marker a page must escape wherever it echoes uploaded text
PROBE = '<b data-probe="1">'

# ---------------------------------------------------------------------------------------------------------------
# CSV text
# ---------------------------------------------------------------------------------------------------------------

SCHOOL_HEADERS = ("school_name", "contact_email", "secondary_email", "notes")
HEADER_SPELLINGS = (
    *SCHOOL_HEADERS,
    "School Name",
    "Email",
    "Coach Email",
    "Team #",
    "Team",
    "1",
    "",
    " school_name ",
    "SCHOOL_NAME",
    "notes\n",
    '"quoted"',
)


def cell() -> st.SearchStrategy[str]:
    return st.one_of(request_text(255), email_text(), st.sampled_from(PAYLOADS), st.just(PROBE))


def csv_bytes(headers: st.SearchStrategy[list[str]]) -> st.SearchStrategy[bytes]:
    """CSV files as spreadsheets and hand edits produce them: any header, ragged rows (missing and extra cells),
    quoted newlines, CRLF or LF, a BOM, and UTF-8, UTF-16 or Latin-1."""

    def write(header: list[str], rows: list[list[str]], newline: str, bom: bool, encoding: str) -> bytes:
        out = io.StringIO()
        writer = csv.writer(out, lineterminator=newline)
        writer.writerow(header)
        writer.writerows(rows)
        text = out.getvalue()
        data = text.encode(encoding, errors="replace")
        return (b"\xef\xbb\xbf" + data) if bom and encoding == "utf-8" else data

    return st.builds(
        write,
        headers,
        st.lists(st.lists(cell(), max_size=6), max_size=6),
        st.sampled_from(("\r\n", "\n", "\r")),
        st.booleans(),
        st.sampled_from(("utf-8", "utf-8", "utf-8", "utf-16", "latin-1")),
    )


SCHOOL_CSV = st.one_of(
    csv_bytes(st.lists(st.sampled_from(HEADER_SPELLINGS), max_size=6)),
    csv_bytes(st.permutations(SCHOOL_HEADERS).map(list)),
    csv_bytes(st.lists(request_text(40), max_size=5)),
    file_bytes(),
)


def _cleaned_by(name: str, values: st.SearchStrategy[str]) -> st.SearchStrategy[str]:
    """Values as the school info form's field cleans them, leaving out those it refuses."""
    form_field = SchoolInfoEditForm.base_fields[name]

    def clean(value: str) -> str | None:
        try:
            return str(form_field.clean(value))
        except ValidationError:
            return None

    return values.map(clean).filter(lambda v: v is not None)  # type: ignore[return-value]


def school_rows() -> st.SearchStrategy[list[dict[str, str]]]:
    """Rows the parser accepts, as it gives them back: each value as the school info form cleans it."""
    row = st.fixed_dictionaries(
        {
            "school_name": _cleaned_by("school_name", request_text(255)),
            "contact_email": st.emails(),
            "secondary_email": st.one_of(st.just(""), st.emails()),
            "notes": _cleaned_by("notes", request_text(500)),
        }
    )
    return st.lists(row, min_size=1, max_size=5)


def _upload(content: bytes, name: str = "schools.csv") -> SimpleUploadedFile:
    return SimpleUploadedFile(name, content, content_type="text/csv")


# ---------------------------------------------------------------------------------------------------------------
# The school list CSV (team.forms.parse_csv_file)
# ---------------------------------------------------------------------------------------------------------------


@given(content=SCHOOL_CSV)
def test_school_list_csv_gives_rows_or_errors_for_any_bytes(content: bytes) -> None:
    result = parse_csv_file(_upload(content))
    assert set(result) == {"rows", "errors", "warnings"}
    assert all(isinstance(m, str) and m for m in result["errors"] + result["warnings"])
    if result["errors"]:
        return
    assert result["rows"]
    for row in result["rows"]:
        assert row["school_name"] and row["school_name"] == row["school_name"].strip()
        validate_email(row["contact_email"])
        if row["secondary_email"]:
            validate_email(row["secondary_email"])


@given(rows=school_rows(), newline=st.sampled_from(("\r\n", "\n")), bom=st.booleans())
def test_school_list_csv_round_trips_valid_rows(rows: list[dict[str, str]], newline: str, bom: bool) -> None:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=SCHOOL_HEADERS, lineterminator=newline)
    writer.writeheader()
    writer.writerows(rows)
    content = out.getvalue().encode("utf-8")

    result = parse_csv_file(_upload((b"\xef\xbb\xbf" if bom else b"") + content))

    assert result["errors"] == []
    assert [dict(r) for r in result["rows"]] == rows


@pytest.fixture
def school_list_event(db: Any) -> Event:
    season = Season.objects.create(name="2026", year=2026, is_active=True)
    for n in range(1, 6):
        Team.objects.create(team_number=n, team_name=f"Team {n:02d}")
    return Event.objects.create(season=season, name="Invitational", event_type="invitational", date=date(2026, 10, 3))


@given(content=st.one_of(SCHOOL_CSV, school_rows().map(lambda rows: _write_school_csv(rows))))
def test_school_list_import_stores_whatever_the_csv_gives(content: bytes, school_list_event: Event) -> None:
    """What parses and validates imports without a database error: names, keys, emails and notes fit."""
    with rolled_back():
        parsed = parse_csv_file(_upload(content))
        if parsed["errors"]:
            return
        checked = validate_csv_data(parsed["rows"], school_list_event)
        if checked["errors"]:
            return
        rows = [
            SchoolListRow(
                team_number=r["team_number"],
                school_name=r["school_name"],
                contact_email=r["contact_email"],
                secondary_email=r.get("secondary_email", ""),
                notes=r.get("notes", ""),
            )
            for r in checked["teams_to_create"]
        ]
        try:
            import_school_list(school_list_event, rows, "gold")
        except ImportBlockedError:
            return
        for row in rows:
            assert Team.objects.get(team_number=row.team_number).school_info.school.name_key == normalize_school_name(
                row.school_name
            )


def _write_school_csv(rows: list[dict[str, str]]) -> bytes:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=SCHOOL_HEADERS)
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue().encode("utf-8")


# ---------------------------------------------------------------------------------------------------------------
# A packet's per-team CSV (packets.views._parse_team_extras_csv)
# ---------------------------------------------------------------------------------------------------------------


def _form_text(text: str) -> str | None:
    """The text as PacketUploadForm's team_extras field cleans it, or None when the form refuses it."""
    field = PacketUploadForm.base_fields["team_extras"]
    try:
        return str(field.clean(text))
    except ValidationError:
        return None


TEAM_EXTRAS_TEXT = st.one_of(
    csv_bytes(st.lists(st.sampled_from(("team", "Team", "api_key", "password", "", "TEAM #", "x")), max_size=4)).map(
        lambda b: b.decode("utf-8", errors="replace")
    ),
    request_text(),
)


@given(text=TEAM_EXTRAS_TEXT)
def test_team_extras_csv_gives_a_mapping_or_nothing_for_any_text(text: str) -> None:
    cleaned = _form_text(text)
    if cleaned is None:
        return
    result = _parse_team_extras_csv(cleaned)
    for team, values in result.items():
        assert team.isdecimal() and str(int(team)) == team
        assert all(isinstance(k, str) and isinstance(v, str) for k, v in values.items())
    assert_storable(column(Packet, "team_extras"), result)


@given(
    teams=st.dictionaries(
        st.integers(0, 99),
        st.lists(request_text(100).map(str.strip), min_size=2, max_size=2),
        min_size=1,
        max_size=5,
    ),
    spelling=st.sampled_from(("Team{:02d}", "Team {}", "{}", "team-{}")),
)
def test_team_extras_csv_round_trips_rows(teams: dict[int, list[str]], spelling: str) -> None:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["team", "api_key", "password"])
    for number, values in teams.items():
        writer.writerow([spelling.format(number), *values])
    cleaned = _form_text(out.getvalue())
    if cleaned is None:  # the form refuses the text (a NUL), so the parser never sees it
        return
    assert _parse_team_extras_csv(cleaned) == {str(n): {"api_key": v[0], "password": v[1]} for n, v in teams.items()}


@given(key=request_text(40), value=st.one_of(request_text(200), st.just(PROBE)))
def test_team_extras_are_escaped_in_the_packet_email(key: str, value: str) -> None:
    html = render_to_string("packets/emails/packet_notification.html", {"team_extras": {key: value}})
    assert PROBE not in html
    if "<" in value:
        assert value not in html


# ---------------------------------------------------------------------------------------------------------------
# Evidence files (scoring.screenshots) and ticket attachments
# ---------------------------------------------------------------------------------------------------------------


@given(upload=uploaded_files())
def test_screenshot_is_read_or_refused_and_typed_from_its_bytes(upload: SimpleUploadedFile) -> None:
    try:
        data, name, mime = read_screenshot(upload)
    except ScreenshotError as refused:
        assert str(refused)
        return
    assert mime == (detect_inline_type(data) or DOWNLOAD_MIME)
    for model in (RedTeamScreenshot, IncidentScreenshot):
        assert_storable(column(model, "filename"), name)
        assert_storable(column(model, "mime_type"), mime)
    response = screenshot_response(data, name)
    if detect_inline_type(data) is None:
        assert response["Content-Disposition"].startswith("attachment")
        assert "sandbox" in response["Content-Security-Policy"]


@given(upload=uploaded_files(), default=st.sampled_from(("", "unnamed")))
def test_an_upload_name_is_storable_text(upload: SimpleUploadedFile, default: str) -> None:
    name = upload_name(upload, default)
    assert len(name) <= 255 and (name or default == "")
    assert not any(unicodedata.category(c) in ("Cc", "Cf") for c in name)
    for model in (TicketAttachment, RedTeamScreenshot, IncidentScreenshot, Packet):
        assert_storable(column(model, "filename"), name or "x")


@given(uploads=st.lists(uploaded_files(), max_size=MAX_SCREENSHOTS + 2))
def test_screenshots_are_read_together_or_refused(uploads: list[SimpleUploadedFile]) -> None:
    try:
        read = read_screenshots(uploads)
    except ScreenshotError:
        assert len(uploads) > MAX_SCREENSHOTS
        return
    assert len(read) == len(uploads)


@given(upload=uploaded_files())
def test_file_size_validator_passes_or_refuses_any_upload(upload: SimpleUploadedFile) -> None:
    try:
        assert validate_file_size(upload) is upload
    except ValidationError:
        assert upload.size and upload.size > 50 * 1024 * 1024


@pytest.fixture
def ticket(db: Any) -> Ticket:
    team = Team.objects.create(team_number=1, team_name="Team 01")
    return Ticket.objects.create(
        team=team, category=TicketCategory.objects.first(), ticket_number="T001-001", title="Help"
    )


@given(upload=st.one_of(st.none(), uploaded_files()))
def test_ticket_attachment_is_saved_or_refused_and_served_safely(
    upload: SimpleUploadedFile | None, ticket: Ticket
) -> None:
    with rolled_back():
        refused = _save_attachment(ticket, upload, "blueteam01")
        if refused is not None:
            assert 400 <= refused.status_code < 500
            return
        attachment = TicketAttachment.objects.get(ticket=ticket)
        assert attachment.filename == upload_name(upload, "")
        assert attachment.mime_type == (mimetypes.guess_type(attachment.filename)[0] or DOWNLOAD_MIME)


# ---------------------------------------------------------------------------------------------------------------
# The upload views
# ---------------------------------------------------------------------------------------------------------------


def _client_for(user: User) -> Client:
    client = Client()
    client.force_login(user)
    return client


def _no_server_error(response: HttpResponse) -> None:
    assert response.status_code < 500
    assert PROBE.encode() not in response.content


@given(
    content=st.one_of(SCHOOL_CSV, st.just(f"school_name,contact_email\n{PROBE},a@b.co\n".encode())),
    name=st.sampled_from(("s.csv", f"{PROBE}.csv", "s.txt")),
)
def test_school_list_upload_view_answers_any_file(
    content: bytes, name: str, school_list_event: Event, gold_team_user: User
) -> None:
    client = _client_for(gold_team_user)
    with rolled_back():
        response = client.post(
            reverse("school_info_import"),
            {"upload": "true", "csv_file": _upload(content, name), "event": str(school_list_event.pk)},
        )
        _no_server_error(response)


PACKET_POST = st.fixed_dictionaries(
    {
        "title": request_text(255),
        "notes": request_text(),
        "team_extras": st.one_of(TEAM_EXTRAS_TEXT, st.just("")),
        "packet_file": uploaded_files(),
    }
)


@given(post=PACKET_POST)
def test_packet_upload_view_answers_any_file(post: dict[str, Any], gold_team_user: User) -> None:
    client = _client_for(gold_team_user)
    with rolled_back():
        _no_server_error(client.post(reverse("upload_packet"), post))


@given(files=st.lists(uploaded_files(), max_size=3), source_ip=st.one_of(st.ip_addresses().map(str), request_text(45)))
def test_incident_report_view_answers_any_screenshots(
    files: list[SimpleUploadedFile], source_ip: str, blue_team_user: User
) -> None:
    Team.objects.get_or_create(team_number=1, defaults={"team_name": "Team 01"})
    client = _client_for(blue_team_user)
    post = {
        "source_ip": source_ip,
        "attack_detected_at": "2026-10-03T10:00",
        "attack_description": "Seen",
        "screenshots": files,
    }
    with rolled_back():
        _no_server_error(client.post(reverse("scoring:submit_incident_report"), post))


@given(files=st.lists(uploaded_files(), max_size=3))
def test_red_finding_view_answers_any_screenshots(files: list[SimpleUploadedFile], red_team_user: User) -> None:
    from scoring.models import AttackType

    team, _ = Team.objects.get_or_create(team_number=1, defaults={"team_name": "Team 01"})
    attack = AttackType.objects.filter(is_active=True).first() or AttackType.objects.create(name="Default")
    client = _client_for(red_team_user)
    post = {
        "attack_type": str(attack.pk),
        "source_ip_type": "single",
        "source_ip": "10.0.0.5",
        "affected_teams": [str(team.pk)],
        "notes": "Found it",
        "screenshots": files,
    }
    with rolled_back():
        _no_server_error(client.post(reverse("scoring:submit_red_score"), post))


@given(upload=uploaded_files())
def test_ticket_attachment_view_answers_any_file(
    upload: SimpleUploadedFile, ticket: Ticket, ticketing_support_user: User
) -> None:
    client = _client_for(ticketing_support_user)
    with rolled_back():
        response = client.post(reverse("ticket_attachment_upload", args=[ticket.ticket_number]), {"attachment": upload})
        _no_server_error(response)
        for attachment in TicketAttachment.objects.filter(ticket=ticket):
            download = client.get(
                reverse("ticket_attachment_download", args=[ticket.ticket_number, attachment.pk]) + "?inline=1"
            )
            if download["Content-Type"] not in {
                "image/png",
                "image/jpeg",
                "image/gif",
                "image/webp",
                "application/pdf",
            }:
                assert download["Content-Disposition"].startswith("attachment")


# ---------------------------------------------------------------------------------------------------------------
# What test_property_coverage checks: each parser it finds, and the tests here that cover it
# ---------------------------------------------------------------------------------------------------------------

PARSERS: dict[str, list[Callable[..., None]]] = {
    "web/team/forms.py:parse_csv_file": [
        test_school_list_csv_gives_rows_or_errors_for_any_bytes,
        test_school_list_csv_round_trips_valid_rows,
        test_school_list_import_stores_whatever_the_csv_gives,
    ],
    "web/core/views.py:_parse_school_info_csv": [test_school_list_upload_view_answers_any_file],
    "web/core/views.py:school_info_import": [test_school_list_upload_view_answers_any_file],
    "web/packets/views.py:_parse_team_extras_csv": [
        test_team_extras_csv_gives_a_mapping_or_nothing_for_any_text,
        test_team_extras_csv_round_trips_rows,
        test_team_extras_are_escaped_in_the_packet_email,
    ],
    "web/packets/views.py:upload_packet": [test_packet_upload_view_answers_any_file],
    "web/scoring/models.py:validate_file_size": [test_file_size_validator_passes_or_refuses_any_upload],
    "web/scoring/screenshots.py:read_screenshot": [test_screenshot_is_read_or_refused_and_typed_from_its_bytes],
    "web/scoring/screenshots.py:read_screenshots": [test_screenshots_are_read_together_or_refused],
    "web/scoring/views/incidents.py:submit_incident_report": [test_incident_report_view_answers_any_screenshots],
    "web/scoring/views/red_team.py:submit_red_score": [test_red_finding_view_answers_any_screenshots],
    "web/ticketing/views/attachments.py:_save_attachment": [
        test_ticket_attachment_is_saved_or_refused_and_served_safely
    ],
    "web/ticketing/views/attachments.py:ticket_attachment_upload": [test_ticket_attachment_view_answers_any_file],
    "web/core/utils.py:upload_name": [test_an_upload_name_is_storable_text],
}
