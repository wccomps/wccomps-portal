from django.urls import path

from . import views

urlpatterns = [
    path("", views.ticket_list, name="ticket_list"),
    path("create/", views.create_ticket, name="create_ticket"),
    path("bulk-claim/", views.tickets_bulk_claim, name="tickets_bulk_claim"),
    path("bulk-resolve/", views.tickets_bulk_resolve, name="tickets_bulk_resolve"),
    path("notifications/", views.ticket_notifications, name="ticket_notifications"),
    path("<str:ticket_number>/", views.ticket_detail, name="ticket_detail"),
    path("<str:ticket_number>/dynamic/", views.ticket_detail_dynamic, name="ticket_detail_dynamic"),
    path("<str:ticket_number>/service-checks/", views.ticket_service_checks, name="ticket_service_checks"),
    path("<str:ticket_number>/comment/", views.ticket_comment, name="ticket_comment"),
    path("<str:ticket_number>/cancel/", views.ticket_cancel, name="ticket_cancel"),
    path("<str:ticket_number>/claim/", views.ticket_claim, name="ticket_claim"),
    path("<str:ticket_number>/unclaim/", views.ticket_unclaim, name="ticket_unclaim"),
    path("<str:ticket_number>/reassign/", views.ticket_reassign, name="ticket_reassign"),
    path("<str:ticket_number>/resolve/", views.ticket_resolve, name="ticket_resolve"),
    path("<str:ticket_number>/reopen/", views.ticket_reopen, name="ticket_reopen"),
    path("<str:ticket_number>/change-category/", views.ticket_change_category, name="ticket_change_category"),
    path(
        "<str:ticket_number>/attachment/upload/",
        views.ticket_attachment_upload,
        name="ticket_attachment_upload",
    ),
    path(
        "<str:ticket_number>/attachment/<int:attachment_id>/",
        views.ticket_attachment_download,
        name="ticket_attachment_download",
    ),
]
