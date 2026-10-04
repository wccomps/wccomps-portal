"""Safe storage and serving for incident and red-team evidence files ("screenshots").

They are served from the portal's own origin, so an HTML/SVG file shown inline would run as script
in the reviewer's (Gold/White Team) session. Any type is accepted, since teams legitimately upload
PDFs, text, CSV, Office and archive files, but the type is decided from the file's bytes, only
raster images and PDFs are shown inline, and everything else is served as a sandboxed download.
"""

from django.core.files.uploadedfile import UploadedFile
from django.http import HttpResponse
from django.utils.http import content_disposition_header

MAX_SCREENSHOT_SIZE = 50 * 1024 * 1024  # largest real upload so far was ~31MB (a .tar.gz)
MAX_SCREENSHOTS = 20  # per submission

# Raster images only: SVG can carry script
_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"%PDF-", "application/pdf"),
)
DOWNLOAD_MIME = "application/octet-stream"


class ScreenshotError(ValueError):
    """An uploaded file was rejected."""


def detect_inline_type(data: bytes) -> str | None:
    """MIME type if the bytes are a raster image or PDF (safe to show inline), else None."""
    for signature, mime in _SIGNATURES:
        if data.startswith(signature):
            return mime
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def read_screenshot(upload: UploadedFile[bytes]) -> tuple[bytes, str, str]:
    """Read an upload. Returns (data, filename, mime to store); raises ScreenshotError if too large.

    The stored MIME type comes from the file's bytes, never from the client.
    """
    name = upload.name or "screenshot.png"
    too_big = f"{name} is larger than {MAX_SCREENSHOT_SIZE // (1024 * 1024)} MB"
    if upload.size is not None and upload.size > MAX_SCREENSHOT_SIZE:
        raise ScreenshotError(too_big)
    data = upload.read(MAX_SCREENSHOT_SIZE + 1)
    if len(data) > MAX_SCREENSHOT_SIZE:
        raise ScreenshotError(too_big)
    return data, name, detect_inline_type(data) or DOWNLOAD_MIME


def read_screenshots(uploads: list[UploadedFile[bytes]]) -> list[tuple[bytes, str, str]]:
    """Read a submission's uploads with read_screenshot; raises ScreenshotError if there are too many.

    Read them before saving the submission, so a rejected file leaves nothing to roll back.
    """
    if len(uploads) > MAX_SCREENSHOTS:
        raise ScreenshotError(f"Maximum {MAX_SCREENSHOTS} screenshots allowed per submission")
    return [read_screenshot(upload) for upload in uploads]


def screenshot_response(data: bytes, filename: str) -> HttpResponse:
    """Serve a stored file: real images/PDFs inline, anything else as a sandboxed download.

    Decided from the bytes, not the stored MIME type, because older rows may hold the client's claimed type.
    """
    inline_type = detect_inline_type(data)
    response = HttpResponse(data, content_type=inline_type or DOWNLOAD_MIME)
    response["Content-Disposition"] = str(
        content_disposition_header(as_attachment=inline_type is None, filename=filename)
    )
    if inline_type != "application/pdf":  # browsers won't render a PDF inside a CSP sandbox
        # Even if a browser renders it as a document, nothing in it may run or load
        response["Content-Security-Policy"] = "sandbox; default-src 'none'; img-src 'self' data:"
    return response
