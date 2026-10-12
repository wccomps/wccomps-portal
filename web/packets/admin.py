from typing import Protocol

from django.contrib import admin, messages
from django.db.models import Count, Q, QuerySet
from django.http import HttpRequest, HttpResponse
from django.utils.html import format_html

from .models import Packet, PacketDistribution


class AnnotatedPacket(Protocol):
    """Protocol for Packet with annotated distribution stats."""

    total_distributions: int
    sent_count: int
    failed_count: int
    downloaded_count: int

    def get_distribution_stats(self) -> dict[str, int]: ...


class PacketDistributionInline(admin.TabularInline[PacketDistribution, Packet]):
    model = PacketDistribution
    extra = 0
    fields = [
        "team",
        "email_status",
        "email_sent_to",
        "email_sent_at",
        "download_count",
        "downloaded_at",
    ]
    readonly_fields = [
        "email_sent_at",
        "downloaded_at",
        "download_count",
    ]
    can_delete = False

    def has_add_permission(self, request: HttpRequest, obj: Packet | None = None) -> bool:
        return False


@admin.register(Packet)
class PacketAdmin(admin.ModelAdmin[Packet]):
    list_display = [
        "title",
        "status",
        "distribution_stats_display",
        "file_info",
        "created_at",
        "uploaded_by",
    ]
    list_filter = ["status", "send_via_email", "web_access_enabled", "created_at"]
    search_fields = ["title", "filename", "uploaded_by", "notes"]
    readonly_fields = [
        "actual_distribution_time",
        "created_at",
        "updated_at",
        "file_size",
        "mime_type",
        "distribution_stats_display",
    ]
    fieldsets = [
        (
            "Packet Information",
            {
                "fields": [
                    "title",
                    "event",
                    "status",
                    "filename",
                    "mime_type",
                    "file_size",
                    "notes",
                    "uploaded_by",
                ]
            },
        ),
        (
            "Distribution Settings",
            {
                "fields": [
                    "send_via_email",
                    "web_access_enabled",
                    "actual_distribution_time",
                ]
            },
        ),
        (
            "Statistics",
            {
                "fields": ["distribution_stats_display"],
            },
        ),
        (
            "Timestamps",
            {
                "fields": ["created_at", "updated_at"],
                "classes": ["collapse"],
            },
        ),
    ]
    inlines = [PacketDistributionInline]
    actions = ["distribute_now", "mark_as_completed", "export_distribution_report"]

    def get_queryset(self, request: HttpRequest) -> QuerySet[Packet]:
        qs = super().get_queryset(request)
        qs = qs.annotate(
            total_distributions=Count("distributions"),
            sent_count=Count("distributions", filter=Q(distributions__email_status="sent")),
            failed_count=Count("distributions", filter=Q(distributions__email_status="failed")),
            downloaded_count=Count(
                "distributions",
                filter=Q(distributions__downloaded_at__isnull=False),
            ),
        )
        return qs

    @admin.display(description="File")
    def file_info(self, obj: Packet) -> str:
        size_kb = obj.file_size / 1024
        size_str = f"{size_kb:.1f} KB" if size_kb < 1024 else f"{size_kb / 1024:.1f} MB"
        return format_html(
            "{}<br><small>{} ({})</small>",
            obj.filename,
            obj.mime_type,
            size_str,
        )

    @admin.display(description="Distribution Stats")
    def distribution_stats_display(self, obj: AnnotatedPacket) -> str:
        if hasattr(obj, "total_distributions"):
            # From annotated queryset
            stats = {
                "total": obj.total_distributions,
                "sent": obj.sent_count,
                "failed": obj.failed_count,
                "downloaded": obj.downloaded_count,
            }
        else:
            stats = obj.get_distribution_stats()

        return format_html(
            '<table style="width: 100%;"><tr>'
            "<td><strong>Total:</strong> {}</td>"
            "<td><strong>Sent:</strong> {}</td>"
            "</tr><tr>"
            "<td><strong>Failed:</strong> {}</td>"
            "<td><strong>Downloaded:</strong> {}</td>"
            "</tr></table>",
            stats["total"],
            stats.get("sent", 0),
            stats.get("failed", 0),
            stats.get("downloaded", 0),
        )

    @admin.action(description="Distribute selected packets now")
    def distribute_now(self, request: HttpRequest, queryset: QuerySet[Packet]) -> None:
        from .services import PacketDistributionService

        count = 0
        for packet in queryset:
            if packet.status == "draft":
                service = PacketDistributionService()
                try:
                    service.distribute_packet(packet)
                except ValueError as e:
                    self.message_user(request, f"{packet.title}: {e}", level=messages.ERROR)
                    continue
                count += 1

        self.message_user(
            request,
            f"Started distribution for {count} team packet(s).",
        )

    @admin.action(description="Mark selected packets as completed")
    def mark_as_completed(self, request: HttpRequest, queryset: QuerySet[Packet]) -> None:
        count = queryset.update(status="completed")
        self.message_user(request, f"Marked {count} packet(s) as completed.")

    @admin.action(description="Export distribution report to CSV")
    def export_distribution_report(self, request: HttpRequest, queryset: QuerySet[Packet]) -> HttpResponse:
        import csv

        from django.http import HttpResponse

        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="packet_distribution_report.csv"'

        writer = csv.writer(response)
        writer.writerow(
            [
                "Packet Title",
                "Team Number",
                "Team Name",
                "Email Status",
                "Email Sent To",
                "Email Sent At",
                "Downloaded",
                "Download Count",
                "Last Downloaded",
            ]
        )

        for packet in queryset:
            for dist in packet.distributions.select_related("team").order_by("team__team_number"):
                writer.writerow(
                    [
                        packet.title,
                        dist.team.team_number,
                        dist.team.team_name,
                        dist.email_status,
                        dist.email_sent_to,
                        dist.email_sent_at.isoformat() if dist.email_sent_at else "",
                        "Yes" if dist.downloaded_at else "No",
                        dist.download_count,
                        dist.last_downloaded_at.isoformat() if dist.last_downloaded_at else "",
                    ]
                )

        return response


@admin.register(PacketDistribution)
class PacketDistributionAdmin(admin.ModelAdmin[PacketDistribution]):
    list_display = [
        "packet",
        "team",
        "email_status",
        "email_sent_to",
        "email_sent_at",
        "download_info",
    ]
    list_filter = [
        "email_status",
        "web_access_enabled",
        "packet__status",
        "email_sent_at",
        "downloaded_at",
    ]
    search_fields = [
        "packet__title",
        "team__team_name",
        "team__team_number",
        "email_sent_to",
        "downloaded_by",
    ]
    readonly_fields = [
        "email_sent_at",
        "downloaded_at",
        "last_downloaded_at",
        "download_count",
        "created_at",
        "updated_at",
    ]
    fieldsets = [
        (
            "Basic Information",
            {
                "fields": ["packet", "team"],
            },
        ),
        (
            "Email Delivery",
            {
                "fields": [
                    "email_status",
                    "email_sent_to",
                    "email_sent_at",
                    "email_error_message",
                ]
            },
        ),
        (
            "Web Access",
            {
                "fields": [
                    "web_access_enabled",
                    "downloaded_at",
                    "download_count",
                    "last_downloaded_at",
                    "downloaded_by",
                ]
            },
        ),
        (
            "Timestamps",
            {
                "fields": ["created_at", "updated_at"],
                "classes": ["collapse"],
            },
        ),
    ]
    actions = ["retry_failed_emails"]

    @admin.display(description="Downloads")
    def download_info(self, obj: PacketDistribution) -> str:
        if obj.download_count > 0:
            return format_html(
                "{} download(s)<br><small>Last: {}</small>",
                obj.download_count,
                obj.last_downloaded_at.strftime("%Y-%m-%d %H:%M") if obj.last_downloaded_at else "Never",
            )
        return "Not downloaded"

    @admin.action(description="Retry sending failed emails")
    def retry_failed_emails(self, request: HttpRequest, queryset: QuerySet[PacketDistribution]) -> None:
        import logging

        from .services import PacketDistributionService, check_resendable

        logger = logging.getLogger(__name__)
        service = PacketDistributionService()
        failed_dists = queryset.filter(email_status="failed")
        count = 0

        for dist in failed_dists:
            if refusal := check_resendable(dist.packet):
                self.message_user(request, f"Team {dist.team.team_number}: {refusal}", level=messages.ERROR)
                continue
            try:
                service.send_packet_email(dist)
                count += 1
            except Exception as e:
                logger.exception(f"Failed to retry email for distribution {dist.id}: {e}")

        self.message_user(
            request,
            f"Retried {count} failed email(s).",
        )
