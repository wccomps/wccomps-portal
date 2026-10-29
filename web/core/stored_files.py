"""Files people upload and the portal stores and serves back: packets, ticket attachments and evidence files.

They are served from the portal's own origin, so an HTML or SVG file shown inline would run as script in the
viewer's session. One policy for all of them:
- a stored file's name is cleaned of characters that can't be stored or that disguise it;
- its type comes from its bytes when they are a raster image or PDF (the only types shown inline), otherwise
  from its name, but never an image or PDF from the name alone;
- file_response serves it inline only when its bytes are such a type, and otherwise as a sandboxed download.
Every model that stores such a file is a StoredFile, which stores it through with_file; pages serve it through serve.
"""

import mimetypes
import unicodedata
from typing import ClassVar, Self

from django.core.files.uploadedfile import UploadedFile
from django.db import models
from django.http import HttpResponse
from django.utils.http import content_disposition_header

MAX_NAME_LENGTH = 255
DOWNLOAD_MIME = "application/octet-stream"

# Raster images only: SVG can carry script
_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"%PDF-", "application/pdf"),
)
# Bidi embeddings, overrides and isolates: they show a name's characters out of order ("evil\u202egnp.exe")
_BIDI_CONTROLS = frozenset("\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")


def clean_name(name: str | None, default: str) -> str:
    """A client's file name as the portal stores it: without control characters (a NUL can't be stored) or bidi
    controls, at most MAX_NAME_LENGTH characters, else default. Other format characters, such as the zero-width
    joiners some scripts and emoji need, stay."""
    kept = "".join(c for c in name or "" if unicodedata.category(c) != "Cc" and c not in _BIDI_CONTROLS)
    return kept.strip()[:MAX_NAME_LENGTH] or default


def detect_inline_type(data: bytes) -> str | None:
    """MIME type if the bytes are a raster image or PDF (safe to show inline), else None."""
    for signature, mime in _SIGNATURES:
        if data.startswith(signature):
            return mime
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def stored_type(data: bytes, name: str) -> str:
    """The type to store for a file: from its bytes when they are an inline type, else from its name, except that
    a name never makes it an image or PDF (pages preview those by the stored type)."""
    if inline_type := detect_inline_type(data):
        return inline_type
    guessed = mimetypes.guess_type(name)[0] or DOWNLOAD_MIME
    return DOWNLOAD_MIME if guessed.startswith("image/") or guessed == "application/pdf" else guessed


def file_response(data: bytes, filename: str, *, inline: bool) -> HttpResponse:
    """Serve a stored file: inline when asked and its bytes are a raster image or PDF, otherwise as a download.

    Decided from the bytes, not a stored type: older rows hold the type the client claimed. Nothing in the response
    may run or load (a CSP sandbox), except a PDF shown inline, which browsers won't render inside a sandbox.
    """
    inline_type = detect_inline_type(data)
    shown = inline and inline_type is not None
    response = HttpResponse(data, content_type=inline_type or DOWNLOAD_MIME)
    response["Content-Disposition"] = str(content_disposition_header(as_attachment=not shown, filename=filename))
    if not (shown and inline_type == "application/pdf"):
        response["Content-Security-Policy"] = "sandbox; default-src 'none'; img-src 'self' data:"
    return response


class UploadError(ValueError):
    """An uploaded file can't be stored; the message says which and why."""


class StoredFile(models.Model):
    DEFAULT_NAME: ClassVar[str]
    MAX_SIZE: ClassVar[int]

    file_data = models.BinaryField()
    filename = models.CharField(max_length=MAX_NAME_LENGTH)
    mime_type = models.CharField(max_length=100)

    class Meta:
        abstract = True

    @classmethod
    def too_big(cls, size: int) -> str:
        """Why a file of this size can't be stored, or ''."""
        return f"File too large (max {cls.MAX_SIZE // (1024 * 1024)}MB)" if size > cls.MAX_SIZE else ""

    @classmethod
    def from_upload(cls, upload: UploadedFile[bytes], **fields: object) -> Self:
        """An unsaved row holding the upload, read at most one byte past MAX_SIZE; UploadError names it when too big."""
        if upload.size is not None and (problem := cls.too_big(upload.size)):
            raise UploadError(f"{clean_name(upload.name, cls.DEFAULT_NAME)}: {problem}")
        data = upload.read(cls.MAX_SIZE + 1)
        if problem := cls.too_big(len(data)):
            raise UploadError(f"{clean_name(upload.name, cls.DEFAULT_NAME)}: {problem}")
        return cls.with_file(data, upload.name, **fields)

    @classmethod
    def with_file(cls, data: bytes, name: str | None, **fields: object) -> Self:
        """An unsaved row holding data, under its cleaned name and the type stored_type gives it."""
        filename = clean_name(name, cls.DEFAULT_NAME)
        return cls(file_data=data, filename=filename, mime_type=stored_type(data, filename), **fields)

    def serve(self, *, inline: bool) -> HttpResponse:
        return file_response(bytes(self.file_data), self.filename, inline=inline)
