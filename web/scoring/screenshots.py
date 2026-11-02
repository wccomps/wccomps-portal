"""Incident and red-team evidence files ("screenshots"): any type is accepted, since teams upload PDFs, text, CSV,
Office and archive files; they are stored and served as core.stored_files.StoredFile says."""

from django.core.files.uploadedfile import UploadedFile

from core.stored_files import StoredFile, UploadError

MAX_SCREENSHOTS = 20  # per submission


def read_screenshots[S: StoredFile](uploads: list[UploadedFile[bytes]], model: type[S]) -> list[S]:
    """A submission's uploads as unsaved rows of model; UploadError when there are too many or one is too big.

    Read them before saving the submission, so a refused file leaves nothing to roll back.
    """
    if len(uploads) > MAX_SCREENSHOTS:
        raise UploadError(f"Maximum {MAX_SCREENSHOTS} screenshots allowed per submission")
    return [model.from_upload(upload) for upload in uploads]
