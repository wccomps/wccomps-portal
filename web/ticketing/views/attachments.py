import logging
from typing import cast

from django.contrib import messages
from django.contrib.auth.models import User
from django.core.exceptions import BadRequest
from django.core.files.uploadedfile import UploadedFile
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect
from django.views.decorators.http import require_POST

from core.auth_utils import team_record_or_404
from core.stored_files import UploadError, clean_name
from ticketing.access import TICKET_STAFF
from ticketing.models import CommentRateLimit, Ticket, TicketAttachment

logger = logging.getLogger(__name__)


def _read_attachment(ticket: Ticket, uploaded_file: UploadedFile[bytes] | None, uploaded_by: str) -> TicketAttachment:
    """The upload as an unsaved attachment; BadRequest says why it is refused."""
    if not uploaded_file:
        raise BadRequest("No file provided")

    try:
        if not clean_name(uploaded_file.name, ""):
            raise BadRequest("File must have a name")
        return TicketAttachment.from_upload(uploaded_file, ticket=ticket, uploaded_by=uploaded_by)
    except UploadError as refused:
        raise BadRequest(str(refused)) from None
    finally:
        uploaded_file.close()


@require_POST
def ticket_attachment_upload(request: HttpRequest, ticket_number: str) -> HttpResponse:
    """Add a file to a ticket, counted against the comment rate limit only once the ticket's state and caps take it
    (TicketAttachment.store); a refusal goes back to the ticket with a message, and a file that can't be read or
    doesn't fit is a bad request."""
    user = cast(User, request.user)
    authentik_username = user.username
    ticket = team_record_or_404(
        Ticket.objects.select_related("team"), user, staff=(TICKET_STAFF,), ticket_number=ticket_number
    )
    attachment = _read_attachment(ticket, request.FILES.get("attachment"), authentik_username)
    refused = attachment.store(lambda: CommentRateLimit.take(ticket, CommentRateLimit.user_key(user)))
    if refused:
        messages.error(request, refused)
    else:
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

    return attachment.serve(inline=request.GET.get("inline") == "1")
