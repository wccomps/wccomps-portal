"""Re-exports every view so callers can use ``from ticketing import views``."""

from .actions import (
    ticket_cancel,
    ticket_change_category,
    ticket_claim,
    ticket_reassign,
    ticket_reopen,
    ticket_resolve,
    ticket_unclaim,
)
from .attachments import ticket_attachment_download, ticket_attachment_upload
from .bulk import tickets_bulk_claim, tickets_bulk_resolve
from .create import create_ticket
from .detail import ticket_comment, ticket_detail, ticket_detail_dynamic
from .list import ticket_list, ticket_notifications
from .ops import ops_batch_verify_tickets, ops_review_tickets

__all__ = [
    "create_ticket",
    "ops_batch_verify_tickets",
    "ops_review_tickets",
    "ticket_attachment_download",
    "ticket_attachment_upload",
    "ticket_cancel",
    "ticket_change_category",
    "ticket_claim",
    "ticket_comment",
    "ticket_detail",
    "ticket_detail_dynamic",
    "ticket_list",
    "ticket_notifications",
    "ticket_reassign",
    "ticket_reopen",
    "ticket_resolve",
    "ticket_unclaim",
    "tickets_bulk_claim",
    "tickets_bulk_resolve",
]
