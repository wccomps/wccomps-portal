from django.contrib import admin

from core.admin import PortalModelAdmin
from orange_team.models import (
    OrangeAssignment,
    OrangeAssignmentResult,
    OrangeCheck,
    OrangeCheckCriterion,
    OrangeCheckIn,
    OrangeFollowUp,
)


class OrangeCheckCriterionInline(admin.TabularInline[OrangeCheckCriterion, OrangeCheck]):
    model = OrangeCheckCriterion
    extra = 1


class OrangeAssignmentResultInline(admin.TabularInline[OrangeAssignmentResult, OrangeAssignment]):
    model = OrangeAssignmentResult
    extra = 0


@admin.register(OrangeCheckIn)
class OrangeCheckInAdmin(PortalModelAdmin[OrangeCheckIn]):
    list_display = ("user", "checked_in_at", "checked_out_at", "is_active")
    list_filter = ("is_active",)
    search_fields = ("user__username",)


@admin.register(OrangeCheck)
class OrangeCheckAdmin(PortalModelAdmin[OrangeCheck]):
    list_display = ("title", "scheduled_at", "created_by", "created_at")
    search_fields = ("title",)
    inlines = [OrangeCheckCriterionInline]


@admin.register(OrangeAssignment)
class OrangeAssignmentAdmin(PortalModelAdmin[OrangeAssignment]):
    list_display = ("orange_check", "user", "team", "status", "score", "submitted_at")
    list_filter = ("status",)
    search_fields = ("user__username", "team__team_name")
    inlines = [OrangeAssignmentResultInline]


@admin.register(OrangeFollowUp)
class OrangeFollowUpAdmin(PortalModelAdmin[OrangeFollowUp]):
    list_display = ("user", "assignment", "remind_at", "dismissed")
    list_filter = ("dismissed",)
