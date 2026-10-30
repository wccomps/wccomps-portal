"""Every model that stores an uploaded file takes it through core.stored_files.StoredFile; pages serve it there."""

from collections.abc import Iterator

import pytest
from django.apps import apps
from django.contrib import admin
from django.contrib.admin.options import BaseModelAdmin
from django.contrib.auth.models import User
from django.db.models import Model
from django.http import Http404, HttpRequest
from django.test import RequestFactory

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


def _stored_file_admins(request: HttpRequest) -> Iterator[tuple[str, BaseModelAdmin[Model], bool]]:
    """Each admin and inline of a StoredFile model, with whether it may add a row."""
    for model, model_admin in admin.site._registry.items():
        if issubclass(model, StoredFile):
            yield model._meta.label, model_admin, model_admin.has_add_permission(request)
        for inline_class in model_admin.inlines:
            if issubclass(inline_class.model, StoredFile):
                inline = inline_class(model, admin.site)
                yield f"{model._meta.label} inline", inline, inline.has_add_permission(request, None)


def test_no_admin_writes_a_stored_files_name_type_or_bytes(admin_user: User) -> None:
    """The admin would store a name or type the policy didn't make, or a row without bytes."""
    request = RequestFactory().get("/")
    request.user = admin_user
    found = {}
    for where, model_admin, can_add in _stored_file_admins(request):
        editable = set(model_admin.get_fields(request)) - set(model_admin.get_readonly_fields(request))
        if written := editable & {"filename", "mime_type", "file_data"}:
            found[where] = f"edits {sorted(written)}"
        elif can_add:
            found[where] = "adds rows"
    assert found == {}


def test_a_stored_file_without_bytes_is_not_found_and_an_empty_one_is_served() -> None:
    model = apps.get_model("scoring.RedTeamScreenshot")
    with pytest.raises(Http404):
        model(file_data=None, filename="lost.png").serve(inline=True)
    assert model(file_data=b"", filename="empty.txt").serve(inline=True).status_code == 200


def test_no_stored_file_claims_a_type_before_its_bytes_are_seen() -> None:
    assert [m._meta.label for m in _file_models() if m._meta.get_field("mime_type").has_default()] == []


def test_every_stored_file_names_its_unnamed_files_and_limits_its_size() -> None:
    assert [m._meta.label for m in _file_models() if not isinstance(getattr(m, "DEFAULT_NAME", None), str)] == []
    assert [m._meta.label for m in _file_models() if not isinstance(getattr(m, "MAX_SIZE", None), int)] == []
