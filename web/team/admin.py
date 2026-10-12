from django.contrib import admin
from django.db.models import QuerySet
from django.forms import ModelForm
from django.http import HttpRequest, HttpResponse
from django.template.loader import render_to_string
from django.utils.html import format_html

from .models import (
    DiscordLink,
    LinkAttempt,
    LinkRateLimit,
    LinkToken,
    SchoolInfo,
    Team,
)


def _password_reset_button(team: Team) -> str:
    if team._state.adding:  # the add page has no team yet
        return "-"
    return render_to_string("admin/includes/team_password_reset.html", {"team_number": team.team_number})


@admin.register(Team)
class TeamAdmin(admin.ModelAdmin[Team]):
    list_display = [
        "team_number",
        "team_name",
        "authentik_group",
        "is_active",
        "member_count",
        "created_at",
    ]
    list_filter = ["is_active", "created_at"]
    search_fields = ["team_name", "authentik_group"]
    ordering = ["team_number"]
    readonly_fields = ["created_at", "updated_at", "ticket_counter", "password_reset"]

    def has_delete_permission(self, request: HttpRequest, obj: Team | None = None) -> bool:
        # Teams are fixed slots from init_teams; deleting one would take its season history with it
        return False

    @admin.display(description="Password")
    def password_reset(self, obj: Team) -> str:
        return _password_reset_button(obj)

    @admin.display(description="Members")
    def member_count(self, obj: Team) -> int:
        return obj.get_member_count()


@admin.register(DiscordLink)
class DiscordLinkAdmin(admin.ModelAdmin[DiscordLink]):
    list_display = [
        "discord_username",
        "get_username",
        "team",
        "is_active",
        "linked_at",
    ]
    list_filter = ["is_active", "team", "linked_at"]
    search_fields = ["discord_username", "user__username"]
    readonly_fields = ["linked_at", "unlinked_at"]
    ordering = ["-linked_at"]

    @admin.display(description="Authentik Username")
    def get_username(self, obj: DiscordLink) -> str:
        return obj.user.username if obj.user else ""


@admin.register(LinkToken)
class LinkTokenAdmin(admin.ModelAdmin[LinkToken]):
    list_display = ["token", "discord_username", "used", "expires_at", "created_at"]
    list_filter = ["used", "expires_at"]
    search_fields = ["discord_username", "token"]
    readonly_fields = ["created_at"]
    ordering = ["-created_at"]


@admin.register(LinkAttempt)
class LinkAttemptAdmin(admin.ModelAdmin[LinkAttempt]):
    list_display = [
        "discord_username",
        "authentik_username",
        "team",
        "success",
        "created_at",
    ]
    list_filter = ["success", "team", "created_at"]
    search_fields = ["discord_username", "authentik_username"]
    readonly_fields = ["created_at"]
    ordering = ["-created_at"]

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False


@admin.register(LinkRateLimit)
class LinkRateLimitAdmin(admin.ModelAdmin[LinkRateLimit]):
    list_display = ["discord_id", "attempted_at"]
    list_filter = ["attempted_at"]
    search_fields = ["discord_id"]
    readonly_fields = ["attempted_at"]
    ordering = ["-attempted_at"]

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False


@admin.register(SchoolInfo)
class SchoolInfoAdmin(admin.ModelAdmin[SchoolInfo]):
    list_display = [
        "school_display",
        "team",
        "contact_email",
        "secondary_email",
        "updated_at",
    ]
    list_select_related = ["school", "team"]
    search_fields = ["school__name", "contact_email", "team__team_name"]
    # password mirrors the team account's Authentik password for packet emails; editing it here would change
    # only this copy, so it's set solely by reset_team_password. The school, and whether a team has one, is set
    # through registration.services (set_team_school, remove_team_school, clear_school_list, import_school_list).
    exclude = ["password"]
    readonly_fields = ["team", "school", "created_at", "updated_at", "updated_by", "password_with_reset"]
    ordering = ["team__team_number"]
    actions = ["export_as_csv", "import_from_csv"]

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: SchoolInfo | None = None) -> bool:
        return False

    def save_model(self, request: HttpRequest, obj: SchoolInfo, form: ModelForm[SchoolInfo], change: bool) -> None:
        # Only what this page edits: a full save could write back a password or school changed since it loaded
        obj.save(update_fields=["contact_email", "secondary_email", "notes", "updated_at"])

    @admin.display(description="School name", ordering="school__name")
    def school_display(self, obj: SchoolInfo) -> str:
        return obj.school_name

    @admin.display(description="Password")
    def password_with_reset(self, obj: SchoolInfo) -> str:
        if obj._state.adding:
            return "-"
        return format_html("<p>{}</p>{}", obj.password or "Not set yet", _password_reset_button(obj.team))

    @admin.action(description="Export as CSV")
    def export_as_csv(self, request: HttpRequest, queryset: QuerySet[SchoolInfo]) -> HttpResponse:
        import csv

        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="school_info.csv"'

        writer = csv.writer(response)
        writer.writerow(
            [
                "team_number",
                "team_name",
                "school_name",
                "contact_email",
                "secondary_email",
                "notes",
            ]
        )

        for school_info in queryset.select_related("team", "school"):
            writer.writerow(
                [
                    school_info.team.team_number,
                    school_info.team.team_name,
                    school_info.school_name,
                    school_info.contact_email,
                    school_info.secondary_email or "",
                    school_info.notes or "",
                ]
            )

        return response

    @admin.action(description="Import from CSV")
    def import_from_csv(self, request: HttpRequest, queryset: QuerySet[SchoolInfo]) -> HttpResponse:
        from django.shortcuts import redirect

        return redirect("/ops/school-info/import/")
