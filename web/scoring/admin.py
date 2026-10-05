from django.contrib import admin
from django.utils.html import format_html

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
    format_boxes_display,
)


@admin.register(AttackType)
class AttackTypeAdmin(admin.ModelAdmin[AttackType]):
    list_display = ["name", "description_short", "is_active", "findings_count", "created_at"]
    list_filter = ["is_active"]
    search_fields = ["name", "description"]
    readonly_fields = ["created_at"]
    ordering = ["name"]

    @admin.display(description="Description")
    def description_short(self, obj: AttackType) -> str:
        return obj.description[:50] + "..." if len(obj.description) > 50 else obj.description

    @admin.display(description="Findings")
    def findings_count(self, obj: AttackType) -> int:
        return obj.findings.count()


class RedTeamScreenshotInline(admin.TabularInline[RedTeamScreenshot, RedTeamScore]):
    model = RedTeamScreenshot
    extra = 1
    fields = ["filename", "description"]


@admin.register(RedTeamScore)
class RedTeamScoreAdmin(admin.ModelAdmin[RedTeamScore]):
    list_display = [
        "id",
        "attack_type",
        "affected_boxes_display",
        "affected_service",
        "team_count",
        "contributors_count",
        "points_per_team",
        "submitted_by",
        "created_at",
    ]
    list_filter = ["attack_type", "universally_attempted", "is_approved", "created_at"]
    search_fields = ["attack_type__name", "affected_service", "notes"]
    autocomplete_fields = ["attack_type"]

    @admin.display(description="Boxes")
    def affected_boxes_display(self, obj: RedTeamScore) -> str:
        return format_boxes_display(obj.affected_boxes) or "—"

    filter_horizontal = ["affected_teams", "contributors"]
    inlines = [RedTeamScreenshotInline]
    readonly_fields = ["created_at", "updated_at"]

    fieldsets = [
        ("Submitted By", {"fields": ["submitted_by", "contributors"]}),
        (
            "Attack Details",
            {
                "fields": [
                    "attack_type",
                    "attack_vector",
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

    @admin.display(description="Contributors")
    def contributors_count(self, obj: RedTeamScore) -> int:
        return obj.contributors.count()


class IncidentScreenshotInline(admin.TabularInline[IncidentScreenshot, IncidentReport]):
    model = IncidentScreenshot
    extra = 1
    fields = ["filename", "description"]


@admin.register(IncidentReport)
class IncidentReportAdmin(admin.ModelAdmin[IncidentReport]):
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
        ("Evidence", {"fields": ["evidence_notes"]}),
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
        return obj.attack_description[:50] + "..." if len(obj.attack_description) > 50 else obj.attack_description

    @admin.display(description="Affected Boxes")
    def affected_boxes_display(self, obj: IncidentReport) -> str:
        return format_boxes_display(obj.affected_boxes) or "—"

    @admin.display(description="Status")
    def reviewed_status(self, obj: IncidentReport) -> str:
        if obj.is_approved:
            return format_html('<span style="color: {};">{}</span>', "green", "✓ Reviewed")
        return format_html('<span style="color: {};">{}</span>', "orange", "Pending")


@admin.register(InjectScore)
class InjectScoreAdmin(admin.ModelAdmin[InjectScore]):
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
        ("Grading", {"fields": ["points_awarded", "notes"]}),
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
class OrangeTeamScoreAdmin(admin.ModelAdmin[OrangeTeamScore]):
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
        return obj.description[:50] + "..." if len(obj.description) > 50 else obj.description


@admin.register(ServiceScore)
class ServiceScoreAdmin(admin.ModelAdmin[ServiceScore]):
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
class ScoringExclusionAdmin(admin.ModelAdmin[ScoringExclusion]):
    list_display = ["team", "created_at"]
    search_fields = ["team__team_name"]
    readonly_fields = ["created_at"]


@admin.register(ScoringTemplate)
class ScoringTemplateAdmin(admin.ModelAdmin[ScoringTemplate]):
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
class QuotientMetadataCacheAdmin(admin.ModelAdmin[QuotientMetadataCache]):
    list_display = ["event_name", "team_count", "last_synced", "synced_by"]
    readonly_fields = ["last_synced"]
    search_fields = ["event_name"]


@admin.register(RedTeamIPPool)
class RedTeamIPPoolAdmin(admin.ModelAdmin[RedTeamIPPool]):
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
