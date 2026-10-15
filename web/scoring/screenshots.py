"""Incident and red-team evidence files ("screenshots"): any type is accepted, since teams upload PDFs, text, CSV,
Office and archive files; they are stored and served as core.stored_files says."""

from django.core.files.uploadedfile import UploadedFile

from core.stored_files import clean_name, stored_type

MAX_SCREENSHOT_SIZE = 50 * 1024 * 1024  # largest real upload so far was ~31MB (a .tar.gz)
MAX_SCREENSHOTS = 20  # per submission


class ScreenshotError(ValueError):
    """An uploaded file was rejected."""


def read_screenshot(upload: UploadedFile[bytes]) -> tuple[bytes, str, str]:
    """Read an upload. Returns (data, filename, mime to store); raises ScreenshotError if too large.

    The stored type is core.stored_files.stored_type's, never the client's claim.
    """
    name = clean_name(upload.name, "screenshot.png")
    too_big = f"{name} is larger than {MAX_SCREENSHOT_SIZE // (1024 * 1024)} MB"
    if upload.size is not None and upload.size > MAX_SCREENSHOT_SIZE:
        raise ScreenshotError(too_big)
    data = upload.read(MAX_SCREENSHOT_SIZE + 1)
    if len(data) > MAX_SCREENSHOT_SIZE:
        raise ScreenshotError(too_big)
    return data, name, stored_type(data, name)


def read_screenshots(uploads: list[UploadedFile[bytes]]) -> list[tuple[bytes, str, str]]:
    """Read a submission's uploads with read_screenshot; raises ScreenshotError if there are too many.

    Read them before saving the submission, so a rejected file leaves nothing to roll back.
    """
    if len(uploads) > MAX_SCREENSHOTS:
        raise ScreenshotError(f"Maximum {MAX_SCREENSHOTS} screenshots allowed per submission")
    return [read_screenshot(upload) for upload in uploads]
