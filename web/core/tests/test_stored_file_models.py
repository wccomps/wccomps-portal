"""Every model that stores an uploaded file takes it through core.stored_files.StoredFile; pages serve it there."""

import pytest
from django.apps import apps

from core.stored_files import StoredFile

pytestmark = pytest.mark.django_db

HTML = b"<html><script>alert(1)</script></html>"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def _file_models() -> list[type]:
    return [m for m in apps.get_models() if any(f.name == "file_data" for f in m._meta.fields)]


def test_every_stored_file_model_is_a_stored_file() -> None:
    models = _file_models()
    assert {m._meta.label for m in models} >= {
        "packets.Packet",
        "ticketing.TicketAttachment",
        "scoring.RedTeamScreenshot",
        "scoring.IncidentScreenshot",
    }
    assert [m._meta.label for m in models if not issubclass(m, StoredFile)] == []


@pytest.mark.parametrize("label", ["packets.Packet", "ticketing.TicketAttachment", "scoring.RedTeamScreenshot"])
def test_a_stored_file_cleans_its_name_and_types_itself_from_its_bytes(label: str) -> None:
    model = apps.get_model(label)
    disguised = model.with_file(HTML, "evil\u202egnp.png")
    assert disguised.filename == "evilgnp.png"
    assert disguised.mime_type == "application/octet-stream"
    assert model.with_file(PNG, "shot.bin").mime_type == "image/png"
    assert model.with_file(PNG, "\x00").filename == model.DEFAULT_NAME


def test_a_stored_file_serves_script_as_a_sandboxed_download() -> None:
    model = apps.get_model("scoring.RedTeamScreenshot")
    response = model.with_file(HTML, "page.html").serve(inline=True)
    assert response["Content-Type"] == "application/octet-stream"
    assert response["Content-Disposition"].startswith("attachment")
    assert "sandbox" in response["Content-Security-Policy"]


def test_every_stored_file_names_its_unnamed_files_and_limits_its_size() -> None:
    assert [m._meta.label for m in _file_models() if not isinstance(getattr(m, "DEFAULT_NAME", None), str)] == []
    assert [m._meta.label for m in _file_models() if not isinstance(getattr(m, "MAX_SIZE", None), int)] == []
