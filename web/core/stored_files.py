"""Files people upload and the portal stores and serves back: packets, ticket attachments and evidence files.

They are served from the portal's own origin, so an HTML or SVG file shown inline would run as script in the
viewer's session. One policy for all of them:
- a stored file's name is cleaned of characters that can't be stored or that disguise it;
- its type comes from its bytes when they are a raster image or PDF (the only types shown inline), otherwise
  from its name, but never an image or PDF from the name alone;
- file_response serves it inline only when its bytes are such a type, and otherwise as a sandboxed download.
"""

import mimetypes
import unicodedata

from django.http import HttpResponse
from django.utils.http import content_disposition_header

# The length of every stored file name column (Packet, TicketAttachment, RedTeamScreenshot, IncidentScreenshot)
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
