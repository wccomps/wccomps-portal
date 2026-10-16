"""Incident/red-team evidence files are never rendered as pages on the portal's origin (stored XSS).

Any file type is still accepted (teams upload PDFs, text, CSV, archives as evidence); only real
images and PDFs are shown inline, decided from the file's bytes rather than the client's claim.
"""

import io

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from scoring.models import IncidentReport, IncidentScreenshot, RedTeamScore, RedTeamScreenshot
from scoring.screenshots import MAX_SCREENSHOT_SIZE, ScreenshotError, read_screenshot
from team.models import Team

pytestmark = pytest.mark.django_db

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
PDF = b"%PDF-1.7\n" + b"\x00" * 64
HTML = b"<html><script>fetch('/ops/admin/competition/action/',{method:'POST'})</script></html>"
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"></svg>'
TEXT = b"team01 pii dump\n"


def _upload(data: bytes, name: str = "shot.png", content_type: str = "image/png") -> SimpleUploadedFile:
    return SimpleUploadedFile(name, data, content_type=content_type)


# --- read_screenshot (upload) ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "claimed", "stored"),
    [
        (PNG, "text/html", "image/png"),
        (JPEG, "image/jpeg", "image/jpeg"),
        (PDF, "application/pdf", "application/pdf"),
        (HTML, "image/png", "application/octet-stream"),
        (SVG, "image/svg+xml", "application/octet-stream"),
        (TEXT, "text/plain", "application/octet-stream"),
    ],
)
def test_stored_type_comes_from_bytes_not_client(payload, claimed, stored):
    data, name, mime = read_screenshot(_upload(payload, "f.bin", claimed))
    assert (data, name, mime) == (payload, "f.bin", stored)


def test_rejects_oversize():
    big = PNG + b"\x00" * MAX_SCREENSHOT_SIZE
    with pytest.raises(ScreenshotError):
        read_screenshot(SimpleUploadedFile("big.png", big, content_type="image/png"))


def test_upload_reads_are_bounded():
    """Unknown-size uploads must not be read into memory without a limit."""
    upload = SimpleUploadedFile("s.png", b"", content_type="image/png")
    upload.file = io.BytesIO(PNG + b"\x00" * (MAX_SCREENSHOT_SIZE * 2))
    upload.size = None
    with pytest.raises(ScreenshotError):
        read_screenshot(upload)


# --- download views (also cover rows stored before the fix) ----------------------------------------


@pytest.fixture
def team():
    return Team.objects.create(team_number=1, team_name="Team 01")


@pytest.fixture
def incident(team, blue_team_user):
    return IncidentReport.objects.create(
        team=team,
        submitted_by=blue_team_user,
        source_ip="10.0.0.9",
        attack_description="x",
        attack_detected_at=timezone.now(),
    )


def _get(user, url):
    client = Client()
    client.force_login(user)
    return client.get(url)


@pytest.mark.parametrize("stored_mime", ["text/html", "image/png"])  # old rows kept the client's claim
def test_stored_html_incident_file_is_downloaded_not_rendered(incident, gold_team_user, stored_mime):
    shot = IncidentScreenshot.objects.create(
        incident=incident, file_data=HTML, filename="pwn.png", mime_type=stored_mime
    )

    response = _get(gold_team_user, reverse("scoring:incident_screenshot", args=[shot.id]))

    assert response.status_code == 200
    assert response["Content-Type"] == "application/octet-stream"
    assert response["Content-Disposition"].startswith("attachment")
    assert "sandbox" in response["Content-Security-Policy"]


def test_png_still_displays_inline(incident, gold_team_user):
    shot = IncidentScreenshot.objects.create(incident=incident, file_data=PNG, filename="a.png", mime_type="image/png")

    response = _get(gold_team_user, reverse("scoring:incident_screenshot", args=[shot.id]))

    assert response["Content-Type"] == "image/png"
    assert response["Content-Disposition"].startswith("inline")
    assert "sandbox" in response["Content-Security-Policy"]


def test_pdf_still_displays_inline(incident, gold_team_user):
    shot = IncidentScreenshot.objects.create(incident=incident, file_data=PDF, filename="IR.pdf", mime_type="x")

    response = _get(gold_team_user, reverse("scoring:incident_screenshot", args=[shot.id]))

    assert response["Content-Type"] == "application/pdf"
    assert response["Content-Disposition"].startswith("inline")


def test_stored_svg_red_file_is_downloaded_not_rendered(red_team_user, gold_team_user):
    finding = RedTeamScore.objects.create(submitted_by=red_team_user, points_per_team=0)
    shot = RedTeamScreenshot.objects.create(finding=finding, file_data=SVG, filename="x.svg", mime_type="image/svg+xml")

    response = _get(gold_team_user, reverse("scoring:red_screenshot", args=[shot.id]))

    assert response["Content-Type"] == "application/octet-stream"
    assert response["Content-Disposition"].startswith("attachment")


# --- upload path end to end ------------------------------------------------------------------------


def _submit_incident(user, upload: SimpleUploadedFile):
    client = Client()
    client.force_login(user)
    return client.post(
        reverse("scoring:submit_incident_report"),
        {
            "source_ip": "10.0.0.9",
            "attack_description": "saw it",
            "attack_detected_at": timezone.now().strftime("%Y-%m-%dT%H:%M"),
            "screenshots": [upload],
        },
    )


def test_disguised_html_upload_is_stored_as_download_only(team, blue_team_user, mock_quotient_client):
    _submit_incident(blue_team_user, _upload(HTML, "shot.png", "image/png"))

    shot = IncidentScreenshot.objects.get()
    assert shot.mime_type == "application/octet-stream"


def test_text_evidence_upload_still_accepted(team, blue_team_user, mock_quotient_client):
    _submit_incident(blue_team_user, _upload(TEXT, "notes.txt", "text/plain"))

    assert IncidentScreenshot.objects.get().filename == "notes.txt"


def test_png_upload_saved_with_detected_type(team, blue_team_user, mock_quotient_client):
    _submit_incident(blue_team_user, _upload(PNG, "shot.png", "text/html"))

    assert IncidentScreenshot.objects.get().mime_type == "image/png"


# --- rejected uploads: an error message and nothing saved, not a 500 ------------------------------


def _submit_red(user, uploads: list[SimpleUploadedFile]):
    from scoring.models import AttackType

    attack = AttackType.objects.create(name="Credential reuse")
    client = Client()
    client.force_login(user)
    return client.post(
        reverse("scoring:submit_red_score"),
        {
            "source_ip_type": "single",
            "source_ip": "10.0.0.9",
            "attack_type": attack.id,
            "affected_teams": [Team.objects.get().id],
            "screenshots": uploads,
        },
    )


@pytest.mark.parametrize("problem", ["too_many", "too_big"])
def test_rejected_incident_upload_shows_the_error(team, blue_team_user, mock_quotient_client, problem, monkeypatch):
    uploads = [_upload(PNG, f"s{n}.png") for n in range(21)] if problem == "too_many" else [_upload(PNG)]
    if problem == "too_big":
        monkeypatch.setattr("scoring.screenshots.MAX_SCREENSHOT_SIZE", 10)
    client = Client()
    client.force_login(blue_team_user)

    response = client.post(
        reverse("scoring:submit_incident_report"),
        {
            "source_ip": "10.0.0.9",
            "attack_description": "saw it",
            "attack_detected_at": timezone.now().strftime("%Y-%m-%dT%H:%M"),
            "screenshots": uploads,
        },
    )

    assert response.status_code == 200
    assert b"File upload failed" in response.content
    assert not IncidentReport.objects.exists()


@pytest.mark.parametrize("problem", ["too_many", "too_big"])
def test_rejected_red_team_upload_shows_the_error(team, red_team_user, mock_quotient_client, problem, monkeypatch):
    uploads = [_upload(PNG, f"s{n}.png") for n in range(21)] if problem == "too_many" else [_upload(PNG)]
    if problem == "too_big":
        monkeypatch.setattr("scoring.screenshots.MAX_SCREENSHOT_SIZE", 10)

    response = _submit_red(red_team_user, uploads)

    assert response.status_code == 200
    assert b"File upload failed" in response.content
    assert not RedTeamScore.objects.exists()


def test_red_team_finding_with_screenshots_is_saved(team, red_team_user, mock_quotient_client):
    response = _submit_red(red_team_user, [_upload(PNG, "a.png"), _upload(PDF, "b.pdf")])

    assert response.status_code == 302
    assert RedTeamScreenshot.objects.filter(finding=RedTeamScore.objects.get()).count() == 2
