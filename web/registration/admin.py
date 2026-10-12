from django.contrib import admin
from django.forms import ModelForm
from django.http import HttpRequest

from .models import (
    Event,
    EventTeamAssignment,
    RegistrationContact,
    RegistrationEventEnrollment,
    Season,
    TeamRegistration,
)


class RegistrationContactInline(admin.TabularInline[RegistrationContact, TeamRegistration]):
    model = RegistrationContact
    extra = 0


class RegistrationEventEnrollmentInline(admin.TabularInline[RegistrationEventEnrollment, TeamRegistration]):
    model = RegistrationEventEnrollment
    extra = 0


@admin.register(TeamRegistration)
class TeamRegistrationAdmin(admin.ModelAdmin[TeamRegistration]):
    list_display = [
        "school_name",
        "status",
        "submitted_at",
        "approved_at",
        "approved_by",
    ]
    list_filter = ["status", "submitted_at", "approved_at"]
    search_fields = ["school_name", "contacts__email", "contacts__name"]
    # The school is set with the team's school through registration.services
    readonly_fields = ["school", "submitted_at", "approved_at", "paid_at", "credentials_sent_at", "edit_token"]
    ordering = ["-submitted_at"]
    inlines = [RegistrationContactInline, RegistrationEventEnrollmentInline]


@admin.register(Season)
class SeasonAdmin(admin.ModelAdmin[Season]):
    list_display = ["name", "year", "is_active", "created_at"]
    list_filter = ["is_active"]
    ordering = ["-year"]


class EventTeamAssignmentInline(admin.TabularInline[EventTeamAssignment, Event]):
    """Shown only: assignments are set through registration.services (set_team_school, remove_team_school,
    clear_school_list, import_school_list)."""

    model = EventTeamAssignment
    extra = 0

    def has_add_permission(self, request: HttpRequest, obj: Event | None = None) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Event | None = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Event | None = None) -> bool:
        return False


@admin.register(Event)
class EventAdmin(admin.ModelAdmin[Event]):
    list_display = [
        "name",
        "season",
        "event_type",
        "date",
        "registration_open",
        "is_active",
        "is_finalized",
    ]
    list_filter = ["season", "event_type", "registration_open", "is_active", "is_finalized"]
    ordering = ["season", "date"]
    inlines = [EventTeamAssignmentInline]
    # Opening an event here would skip recording the school list at it; the school list import opens events
    readonly_fields = ["is_active"]

    def save_model(self, request: HttpRequest, obj: Event, form: ModelForm[Event], change: bool) -> None:
        if not change:
            super().save_model(request, obj, form, change)
            return
        # A full save would write back the is_active read with the page, undoing an import that opened it since
        obj.save(update_fields=[f.name for f in Event._meta.concrete_fields if f.name not in ("id", "is_active")])


@admin.register(RegistrationContact)
class RegistrationContactAdmin(admin.ModelAdmin[RegistrationContact]):
    list_display = ["name", "email", "role", "registration"]
    list_filter = ["role"]
    search_fields = ["name", "email", "registration__school_name"]
