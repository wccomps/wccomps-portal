import logging
from typing import cast

from django.contrib.auth.models import User
from django.core.exceptions import BadRequest
from django.core.files.uploadedfile import UploadedFile
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect
from django.views.decorators.http import require_POST

from core.auth_utils import team_record_or_404
from core.stored_files import clean_name, file_response, stored_type
from ticketing.access import TICKET_STAFF
from ticketing.models import Ticket, TicketAttachment

logger = logging.getLogger(__name__)

MAX_ATTACHMENT_SIZE = 10 * 1024 * 1024  # 10MB


def _save_attachment(ticket: Ticket, uploaded_file: UploadedFile[bytes] | None, uploaded_by: str) -> None:
    """Validate and save an attachment; BadRequest says why one is refused."""
    if not uploaded_file:
        raise BadRequest("No file provided")

    if uploaded_file.size is None or uploaded_file.size > MAX_ATTACHMENT_SIZE:
        uploaded_file.close()
        raise BadRequest("File too large (max 10MB)")

    filename = clean_name(uploaded_file.name, "")
    if not filename:
        uploaded_file.close()
        raise BadRequest("File must have a name")

    try:
        data = uploaded_file.read()
        TicketAttachment.objects.create(
            ticket=ticket,
            file_data=data,
            filename=filename,
            mime_type=stored_type(data, filename),
            uploaded_by=uploaded_by,
        )
    finally:
        uploaded_file.close()


@require_POST
def ticket_attachment_upload(request: HttpRequest, ticket_number: str) -> HttpResponse:
    user = cast(User, request.user)
    authentik_username = user.username
    ticket = team_record_or_404(
        Ticket.objects.select_related("team"), user, staff=(TICKET_STAFF,), ticket_number=ticket_number
    )
    _save_attachment(ticket, request.FILES.get("attachment"), authentik_username)

    logger.info(f"Attachment uploaded to ticket {ticket.ticket_number} by {authentik_username}")

    return redirect("ticket_detail", ticket_number=ticket.ticket_number)


def ticket_attachment_download(
    request: HttpRequest,
    attachment_id: int,
    ticket_number: str,
) -> HttpResponse:
    attachment = team_record_or_404(
        TicketAttachment.objects.select_related("ticket", "ticket__team"),
        cast(User, request.user),
        staff=(TICKET_STAFF,),
        team_field="ticket__team",
        id=attachment_id,
        ticket__ticket_number=ticket_number,
    )

    return file_response(bytes(attachment.file_data), attachment.filename, inline=request.GET.get("inline") == "1")
