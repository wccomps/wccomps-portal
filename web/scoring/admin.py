from typing import Protocol

from django.contrib import admin
from django.db.models import Count, QuerySet
from django.http import HttpRequest
from django.utils.html import format_html

from core.admin import PortalModelAdmin
from core.utils import shorten

from .models import (
    AttackType,
    IncidentReport,
    IncidentScreenshot,
    InjectScore,
    OrangeTeamScore,
    QuotientMetadataCache,
    RedTeamIPPool,
    RedTeamScore,
    RedTeamScreenshot,
    ScoringExclusion,
    ScoringTemplate,
    ServiceScore,
)


class CountedAttackType(Protocol):
    """An attack type with its findings counted by the admin's queryset."""

    findings_total: int


@admin.register(AttackType)
class AttackTypeAdmin(PortalModelAdmin[AttackType]):
    list_display = ["name", "description_short", "is_active", "findings_count", "created_at"]
    list_filter = ["is_active"]
    search_fields = ["name", "description"]
    readonly_fields = ["created_at"]
    ordering = ["name"]

    @admin.display(description="Description")
    def description_short(self, obj: AttackType) -> str:
        return shorten(obj.description, 50)

    def get_queryset(self, request: HttpRequest) -> QuerySet[AttackType]:
        return super().get_queryset(request).annotate(findings_total=Count("findings"))

    @admin.display(description="Findings", ordering="findings_total")
    def findings_count(self, obj: CountedAttackType) -> int:
        return obj.findings_total


class RedTeamScreenshotInline(admin.TabularInline[RedTeamScreenshot, RedTeamScore]):
    model = RedTeamScreenshot
    extra = 0
    fields = ["filename"]
    readonly_fields = ["filename"]

    def has_add_permission(self, request: HttpRequest, obj: RedTeamScore | None = None) -> bool:
        return False


@admin.register(RedTeamScore)
class RedTeamScoreAdmin(PortalModelAdmin[RedTeamScore]):
    list_display = [
        "id",
        "attack_type",
        "affected_boxes_display",
        "affected_service",
        "team_count",
        "points_per_team",
        "submitted_by",
        "created_at",
    ]
    list_filter = ["attack_type", "universally_attempted", "is_approved", "created_at"]
    search_fields = ["attack_type__name", "affected_service", "notes"]
    autocomplete_fields = ["attack_type"]
    filter_horizontal = ["affected_teams"]
    inlines = [RedTeamScreenshotInline]
    readonly_fields = ["created_at", "updated_at"]

    fieldsets = [
        ("Submitted By", {"fields": ["submitted_by"]}),
        (
            "Attack Details",
            {
                "fields": [
                    "attack_type",
                    "source_ip",
                    "source_ip_pool",
                    "destination_ip_template",
                    "affected_boxes",
                    "affected_service",
                ]
            },
        ),
        (
            "Flags",
            {"fields": ["universally_attempted", "persistence_established"]},
        ),
        (
            "Scoring",
            {"fields": ["affected_teams", "points_per_team", "is_approved", "approved_by", "approved_at"]},
        ),
        ("Evidence", {"fields": ["notes"]}),
        ("Audit", {"fields": ["created_at", "updated_at"]}),
    ]

    @admin.display(description="Teams")
    def team_count(self, obj: RedTeamScore) -> int:
        return obj.affected_teams.count()


class IncidentScreenshotInline(admin.TabularInline[IncidentScreenshot, IncidentReport]):
    model = IncidentScreenshot
    extra = 0
    fields = ["filename"]
    readonly_fields = ["filename"]

    def has_add_permission(self, request: HttpRequest, obj: IncidentReport | None = None) -> bool:
        return False


@admin.register(IncidentReport)
class IncidentReportAdmin(PortalModelAdmin[IncidentReport]):
    list_display = [
        "id",
        "team",
        "attack_description_short",
        "affected_boxes_display",
        "affected_service",
        "attack_detected_at",
        "reviewed_status",
        "points_returned",
        "created_at",
    ]
    list_filter = [
        "team",
        "is_approved",
        "attack_mitigated",
        "attack_detected_at",
    ]
    search_fields = ["attack_description", "affected_service", "team__team_name"]
    readonly_fields = ["created_at", "updated_at"]
    inlines = [IncidentScreenshotInline]

    fieldsets = [
        ("Report Info", {"fields": ["team", "submitted_by"]}),
        (
            "Attack Details",
            {
                "fields": [
                    "attack_description",
                    "source_ip",
                    "destination_ip",
                    "affected_boxes",
                    "affected_service",
                ]
            },
        ),
        (
            "Timeline",
            {"fields": ["attack_detected_at", "attack_mitigated"]},
        ),
        (
            "Gold Team Review",
            {
                "fields": [
                    "is_approved",
                    "matched_to_red_score",
                    "points_returned",
                    "approval_notes",
                    "approved_by",
                    "approved_at",
                ]
            },
        ),
        ("Audit", {"fields": ["created_at", "updated_at"]}),
    ]

    @admin.display(description="Description")
    def attack_description_short(self, obj: IncidentReport) -> str:
        return shorten(obj.attack_description, 50)

    @admin.display(description="Status")
    def reviewed_status(self, obj: IncidentReport) -> str:
        if obj.is_approved:
            return format_html('<span style="color: {};">{}</span>', "green", "Reviewed")
        return format_html('<span style="color: {};">{}</span>', "orange", "Pending")


@admin.register(InjectScore)
class InjectScoreAdmin(PortalModelAdmin[InjectScore]):
    list_display = [
        "team",
        "inject_name",
        "points_awarded",
        "max_points",
        "percentage",
        "graded_by",
        "graded_at",
    ]
    list_filter = ["inject_name", "graded_at"]
    search_fields = ["team__team_name", "inject_name", "inject_id"]
    readonly_fields = ["graded_at", "updated_at"]

    fieldsets = [
        ("Team", {"fields": ["team"]}),
        ("Inject", {"fields": ["inject_id", "inject_name", "max_points"]}),
        ("Grading", {"fields": ["points_awarded"]}),
        ("Audit", {"fields": ["graded_by", "graded_at", "updated_at"]}),
    ]

    @admin.display(description="%")
    def percentage(self, obj: InjectScore) -> str:
        if obj.max_points and obj.max_points > 0:
            pct = (obj.points_awarded / obj.max_points) * 100
            color = "green" if pct >= 80 else "orange" if pct >= 60 else "red"
            return format_html('<span style="color: {};">{}%</span>', color, f"{pct:.1f}")
        return "N/A"


@admin.register(OrangeTeamScore)
class OrangeTeamScoreAdmin(PortalModelAdmin[OrangeTeamScore]):
    list_display = [
        "team",
        "description_short",
        "points_awarded",
        "submitted_by",
        "created_at",
    ]
    list_filter = ["created_at"]
    search_fields = ["team__team_name", "description"]
    readonly_fields = ["created_at", "updated_at"]

    @admin.display(description="Description")
    def description_short(self, obj: OrangeTeamScore) -> str:
        return shorten(obj.description, 50)


@admin.register(ServiceScore)
class ServiceScoreAdmin(PortalModelAdmin[ServiceScore]):
    list_display = [
        "team",
        "service_points",
        "sla_violations",
        "net_score",
        "synced_at",
        "synced_by",
    ]
    list_filter = ["synced_at"]
    search_fields = ["team__team_name"]
    readonly_fields = ["synced_at"]

    @admin.display(description="Net")
    def net_score(self, obj: ServiceScore) -> str:
        total = obj.service_points + obj.sla_violations
        color = "green" if total > 0 else "red" if total < 0 else "black"
        return format_html('<span style="color: {};">{}</span>', color, f"{total:.2f}")


@admin.register(ScoringExclusion)
class ScoringExclusionAdmin(PortalModelAdmin[ScoringExclusion]):
    list_display = ["team", "created_at"]
    search_fields = ["team__team_name"]
    readonly_fields = ["created_at"]


@admin.register(ScoringTemplate)
class ScoringTemplateAdmin(PortalModelAdmin[ScoringTemplate]):
    list_display = [
        "service_weight",
        "inject_weight",
        "orange_weight",
        "updated_at",
    ]
    # The maxima are derived from competition data (service sync, inject grade/metadata
    # sync, orange checks), not hand-edited, so they are read-only here too.
    readonly_fields = ["service_max", "inject_max", "orange_max", "created_at", "updated_at"]

    fieldsets = [
        (
            "Category Weights",
            {
                "fields": [
                    "service_weight",
                    "inject_weight",
                    "orange_weight",
                ],
                "description": "Percentage weights for each category (must sum to 100).",
            },
        ),
        (
            "Raw Maximums",
            {
                "fields": [
                    "service_max",
                    "inject_max",
                    "orange_max",
                ]
            },
        ),
        ("Audit", {"fields": ["updated_by", "created_at", "updated_at"]}),
    ]


@admin.register(QuotientMetadataCache)
class QuotientMetadataCacheAdmin(PortalModelAdmin[QuotientMetadataCache]):
    list_display = ["last_synced", "synced_by"]
    readonly_fields = ["last_synced"]


@admin.register(RedTeamIPPool)
class RedTeamIPPoolAdmin(PortalModelAdmin[RedTeamIPPool]):
    list_display = ["name", "ip_count", "findings_count", "created_by", "created_at"]
    search_fields = ["name", "created_by__username"]
    readonly_fields = ["created_at", "updated_at"]
    list_filter = ["created_by", "created_at"]

    @admin.display(description="IPs")
    def ip_count(self, obj: RedTeamIPPool) -> int:
        return obj.ip_count

    @admin.display(description="Findings")
    def findings_count(self, obj: RedTeamIPPool) -> int:
        return obj.findings.count()
