"""Incident and red-team evidence files ("screenshots"): any type is accepted, since teams upload PDFs, text, CSV,
Office and archive files; they are stored and served as core.stored_files.StoredFile says."""

from django.core.files.uploadedfile import UploadedFile

from core.stored_files import StoredFile, UploadError

MAX_SCREENSHOTS = 20  # per submission
MAX_SUBMISSION_SIZE = 50 * 1024 * 1024


def read_screenshots[S: StoredFile](uploads: list[UploadedFile[bytes]], model: type[S]) -> list[S]:
    """A submission's uploads as unsaved rows of model; UploadError when there are too many or they are too big.

    Read them before saving the submission, so a refused file leaves nothing to roll back.
    """
    if len(uploads) > MAX_SCREENSHOTS:
        raise UploadError(f"Maximum {MAX_SCREENSHOTS} screenshots allowed per submission")
    rows: list[S] = []
    total = 0
    for upload in uploads:
        row = model.from_upload(upload)
        total += len(bytes(row.file_data))
        if total > MAX_SUBMISSION_SIZE:
            raise UploadError(f"Total upload size cannot exceed {MAX_SUBMISSION_SIZE // (1024 * 1024)}MB")
        rows.append(row)
    return rows
