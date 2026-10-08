from django import forms
from django.contrib import admin
from django.db.models import QuerySet
from django.http import HttpRequest, HttpResponse

from .models import (
    CommentRateLimit,
    Ticket,
    TicketAttachment,
    TicketCategory,
    TicketComment,
    TicketHistory,
)

TICKET_FIELD_CHOICES = [
    ("hostname", "Hostname"),
    ("ip_address", "IP Address"),
    ("service_name", "Service Name"),
    ("description", "Description"),
]


class CheckboxSelectMultipleJSON(forms.CheckboxSelectMultiple):
    """Checkbox widget that stores values as a JSON list."""


class TicketCategoryForm(forms.ModelForm[TicketCategory]):
    """Custom form for TicketCategory with checkbox widgets for JSON fields."""

    required_fields = forms.MultipleChoiceField(
        choices=TICKET_FIELD_CHOICES,
        widget=CheckboxSelectMultipleJSON,
        required=False,
        help_text="Fields the user must fill in when creating a ticket.",
    )
    optional_fields = forms.MultipleChoiceField(
        choices=TICKET_FIELD_CHOICES,
        widget=CheckboxSelectMultipleJSON,
        required=False,
        help_text="Fields shown but not required.",
    )

    class Meta:
        model = TicketCategory
        fields = "__all__"
        widgets = {
            "variable_cost_note": forms.Textarea(
                attrs={
                    "placeholder": 'e.g. "If consultation exceeded 45 minutes, '
                    'ticket lead will manually adjust to 300 points"',
                }
            ),
            "playbook": forms.Textarea(
                attrs={
                    "rows": 4,
                    "placeholder": "1. Verify service status\n2. Check credentials\n3. Restart daemon",
                }
            ),
        }

    def clean_required_fields(self) -> list[str]:
        """Return the selected values as a list for JSON storage."""
        return list(self.cleaned_data.get("required_fields", []))

    def clean_optional_fields(self) -> list[str]:
        """Return the selected values as a list for JSON storage."""
        return list(self.cleaned_data.get("optional_fields", []))

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        if self.instance and self.instance.pk:
            if self.instance.required_fields:
                self.initial["required_fields"] = self.instance.required_fields
            if self.instance.optional_fields:
                self.initial["optional_fields"] = self.instance.optional_fields


class TicketAttachmentInline(admin.TabularInline[TicketAttachment, Ticket]):
    model = TicketAttachment
    extra = 0
    readonly_fields = ["uploaded_at", "uploaded_by", "filename", "mime_type"]
    fields = ["filename", "mime_type", "uploaded_by", "uploaded_at"]


class TicketCommentInline(admin.TabularInline[TicketComment, Ticket]):
    model = TicketComment
    extra = 0
    readonly_fields = ["posted_at", "author"]
    fields = ["author", "comment_text", "posted_at"]


class TicketHistoryInline(admin.TabularInline[TicketHistory, Ticket]):
    model = TicketHistory
    extra = 0
    readonly_fields = ["timestamp", "action", "actor"]
    fields = ["action", "actor", "details", "timestamp"]


@admin.register(TicketCategory)
class TicketCategoryAdmin(admin.ModelAdmin[TicketCategory]):
    form = TicketCategoryForm
    list_display = ["display_name", "points", "get_steps_count", "variable_points", "user_creatable", "sort_order"]
    list_filter = ["variable_points", "user_creatable"]
    ordering = ["sort_order", "display_name"]
    fieldsets = (
        (None, {"fields": ("display_name", "points", "playbook", "sort_order")}),
        (
            "Ticket Fields",
            {
                "fields": ("required_fields", "optional_fields"),
            },
        ),
        (
            "Variable Points",
            {
                "fields": (
                    "variable_points",
                    "variable_cost_note",
                    "min_points",
                    "max_points",
                ),
                "description": "Configure when point costs vary per ticket.",
            },
        ),
        ("Visibility", {"fields": ("user_creatable",)}),
    )

    @admin.display(description="Playbook")
    def get_steps_count(self, obj: TicketCategory) -> str:
        count = len(obj.steps)
        return f"{count} step{'s' if count != 1 else ''}" if count else "—"


@admin.register(Ticket)
class TicketAdmin(admin.ModelAdmin[Ticket]):
    list_display = [
        "ticket_number",
        "team",
        "category",
        "status",
        "get_assigned_to_display",
        "points_charged",
        "created_at",
    ]
    list_filter = ["status", "category", "team"]
    search_fields = ["ticket_number", "title", "hostname", "service_name"]
    ordering = ["-created_at"]
    readonly_fields = ["created_at", "updated_at"]
    inlines = [TicketAttachmentInline, TicketCommentInline, TicketHistoryInline]
    actions = ["export_as_csv"]

    def get_readonly_fields(self, request: HttpRequest, obj: Ticket | None = None) -> list[str]:
        """Make ticket_number read-only on existing tickets."""
        if obj:
            return [*self.readonly_fields, "ticket_number"]
        return list(self.readonly_fields)

    fieldsets = (
        ("Identity", {"fields": ("ticket_number", "team", "status")}),
        (
            "Content",
            {
                "fields": (
                    "category",
                    "title",
                    "description",
                    "hostname",
                    "ip_address",
                    "service_name",
                    "custom_fields",
                )
            },
        ),
        (
            "Assignment",
            {
                "fields": (
                    "assigned_to",
                    "assigned_at",
                )
            },
        ),
        (
            "Resolution",
            {
                "fields": (
                    "resolved_by",
                    "resolved_at",
                    "resolution_notes",
                    "duration_notes",
                    "points_charged",
                )
            },
        ),
        (
            "Discord",
            {
                "fields": (
                    "discord_thread_id",
                    "discord_channel_id",
                    "thread_archive_scheduled_at",
                )
            },
        ),
        ("Audit", {"fields": ("created_at", "updated_at")}),
    )

    def save_model(self, request: HttpRequest, obj: Ticket, form: object, change: bool) -> None:
        """Auto-generate ticket number for new tickets created via admin."""
        if not change and not obj.ticket_number:
            from django.db.models import F

            from team.models import Team

            team = Team.objects.select_for_update().get(pk=obj.team_id)
            team.ticket_counter = F("ticket_counter") + 1
            team.save(update_fields=["ticket_counter"])
            team.refresh_from_db()
            obj.ticket_number = f"T{team.team_number:03d}-{team.ticket_counter:03d}"
        super().save_model(request, obj, form, change)

    @admin.display(description="Assigned To")
    def get_assigned_to_display(self, obj: Ticket) -> str:
        return str(obj.assigned_to) if obj.assigned_to else ""

    @admin.action(description="Export selected tickets as CSV")
    def export_as_csv(self, request: HttpRequest, queryset: QuerySet[Ticket]) -> HttpResponse:
        import csv

        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="tickets.csv"'

        writer = csv.writer(response)
        writer.writerow(
            [
                "Ticket Number",
                "Team",
                "Team Number",
                "Category",
                "Title",
                "Description",
                "Status",
                "Hostname",
                "IP Address",
                "Service Name",
                "Assigned To",
                "Points Charged",
                "Created At",
                "Resolved At",
                "Resolution Notes",
                "Duration Notes",
            ]
        )

        for ticket in queryset:
            assigned_to_name = str(ticket.assigned_to) if ticket.assigned_to else ""
            writer.writerow(
                [
                    ticket.ticket_number,
                    ticket.team.team_name,
                    ticket.team.team_number,
                    ticket.category.display_name if ticket.category else "",
                    ticket.title,
                    ticket.description,
                    ticket.status,
                    ticket.hostname or "",
                    ticket.ip_address or "",
                    ticket.service_name or "",
                    assigned_to_name,
                    ticket.points_charged,
                    ticket.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                    ticket.resolved_at.strftime("%Y-%m-%d %H:%M:%S") if ticket.resolved_at else "",
                    ticket.resolution_notes or "",
                    ticket.duration_notes or "",
                ]
            )

        return response


@admin.register(TicketAttachment)
class TicketAttachmentAdmin(admin.ModelAdmin[TicketAttachment]):
    list_display = ["filename", "ticket", "uploaded_by", "uploaded_at"]
    list_filter = ["uploaded_at"]
    search_fields = ["filename", "ticket__ticket_number"]
    ordering = ["-uploaded_at"]
    readonly_fields = ["uploaded_at"]


@admin.register(TicketComment)
class TicketCommentAdmin(admin.ModelAdmin[TicketComment]):
    list_display = ["ticket", "author", "posted_at"]
    list_filter = ["posted_at"]
    search_fields = ["ticket__ticket_number", "comment_text"]
    ordering = ["-posted_at"]
    readonly_fields = ["posted_at"]


@admin.register(TicketHistory)
class TicketHistoryAdmin(admin.ModelAdmin[TicketHistory]):
    list_display = ["ticket", "action", "actor", "timestamp"]
    list_filter = ["action", "timestamp"]
    search_fields = ["ticket__ticket_number"]
    ordering = ["-timestamp"]
    readonly_fields = ["timestamp"]


@admin.register(CommentRateLimit)
class CommentRateLimitAdmin(admin.ModelAdmin[CommentRateLimit]):
    list_display = ["ticket", "discord_id", "posted_at"]
    list_filter = ["posted_at"]
    search_fields = ["ticket__ticket_number", "discord_id"]
    ordering = ["-posted_at"]
    readonly_fields = ["posted_at"]

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False
