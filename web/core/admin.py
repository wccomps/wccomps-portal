from contextlib import suppress
from functools import wraps
from typing import TYPE_CHECKING, cast

from django import forms
from django.contrib import admin
from django.contrib.admin.exceptions import DisallowedModelAdminToField
from django.contrib.admin.options import TO_FIELD_VAR
from django.contrib.auth import REDIRECT_FIELD_NAME
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Model, QuerySet
from django.http import HttpRequest, HttpResponse, HttpResponseBase
from django.urls import reverse

if TYPE_CHECKING:
    from django.contrib.admin.sites import _ViewType

from .auth_utils import uses_django_admin
from .models import (
    AuditLog,
    BotState,
    CompetitionConfig,
    DiscordTask,
    UserGroups,
)


class AuthentikAdminSite(admin.AdminSite):
    """Admin site that checks Authentik groups for access."""

    site_header = "WCComps Portal Administration"
    site_title = "WCComps Portal Admin"
    index_title = "Competition Management"
    site_url = "/ops/tickets/"

    def has_permission(self, request: HttpRequest) -> bool:
        return uses_django_admin(request.user)

    def _refuse(self, request: HttpRequest) -> None:
        """The 403 for a signed-in non-admin: the login form is no use, signing in is Authentik's."""
        if request.user.is_authenticated and not self.has_permission(request):
            raise PermissionDenied

    def login(self, request: HttpRequest, extra_context: dict[str, object] | None = None) -> HttpResponse:
        """Signing in is Authentik's: a visitor goes to the portal's sign-in, keeping where the admin sent them."""
        if not request.user.is_authenticated:
            return redirect_to_login(request.GET.get(REDIRECT_FIELD_NAME) or reverse("admin:index"))
        self._refuse(request)
        return super().login(request, extra_context)

    # The stubs bound _ViewType by Callable[..., HttpResponseBase], whose ... mypy counts as explicit Any
    def admin_view(self, view: _ViewType, cacheable: bool = False) -> _ViewType:  # type: ignore[explicit-any]
        inner = super().admin_view(view, cacheable)

        @wraps(inner)
        def refusing(request: HttpRequest, *args: object, **kwargs: object) -> HttpResponseBase:
            # Django refuses a _to_field it can't reference with a 400, but its change and delete pages look an empty
            # one up as a field named "" and fail with a 500. Read as Django reads it: POST, then GET.
            # test_view_regressions' canary fails once Django fixes it; then drop the _to_field check.
            self._refuse(request)
            if request.POST.get(TO_FIELD_VAR, request.GET.get(TO_FIELD_VAR)) == "":
                raise DisallowedModelAdminToField("An empty _to_field cannot be referenced.")
            return inner(request, *args, **kwargs)

        return cast("_ViewType", refusing)  # type: ignore[explicit-any]


admin.site = AuthentikAdminSite()
admin.sites.site = admin.site


class PortalModelAdmin[ModelT: Model](admin.ModelAdmin[ModelT]):
    """Every portal model's admin (test_every_admin_is_a_portal_model_admin checks)."""

    # The stubs say text, but a form's initial value is any value its field takes (a datetime for a date widget)
    def get_changeform_initial_data(self, request: HttpRequest) -> dict[str, object]:  # type: ignore[override]
        """An add form's values from its query (Django's prefill), as Django gives them, but where a widget reads the
        value and fails the page (a 500) on one its field refuses: a date-and-time field gets the text read as a date
        and time, and a related record's field keeps the value only when it names a record (a number past Python's
        digit limit failed the select)."""
        fields = self.get_form(request).base_fields
        initial: dict[str, object] = {}
        for name, value in super().get_changeform_initial_data(request).items():
            field = fields.get(name)
            if isinstance(field, forms.DateTimeField | forms.SplitDateTimeField):
                with suppress(ValidationError):
                    initial[name] = forms.DateTimeField().clean(value)
            elif isinstance(field, forms.ModelChoiceField):
                with suppress(ValidationError):
                    field.clean(value)
                    initial[name] = value
            else:
                initial[name] = value
        return initial


@admin.register(AuditLog)
class AuditLogAdmin(PortalModelAdmin[AuditLog]):
    list_display = ["action", "admin_user", "target_entity", "target_id", "created_at"]
    list_filter = ["action", "target_entity"]
    search_fields = ["admin_user", "action"]
    ordering = ["-created_at"]
    readonly_fields = ["created_at"]

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: AuditLog | None = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: AuditLog | None = None) -> bool:
        return False


@admin.register(DiscordTask)
class DiscordTaskAdmin(PortalModelAdmin[DiscordTask]):
    list_display = ["task_type", "status", "retry_count", "created_at", "completed_at"]
    list_filter = ["status", "task_type"]
    search_fields = ["task_type", "error_message"]
    ordering = ["-created_at"]
    readonly_fields = ["result", "created_at", "completed_at"]

    actions = ["retry_failed_tasks"]

    @admin.action(description="Retry failed tasks")
    def retry_failed_tasks(self, request: HttpRequest, queryset: QuerySet[DiscordTask]) -> None:
        from django.utils import timezone

        updated = queryset.filter(status="failed").update(
            status="pending",
            retry_count=0,
            next_retry_at=timezone.now(),
            error_message="",
        )
        self.message_user(request, f"{updated} tasks reset for retry")


@admin.register(BotState)
class BotStateAdmin(PortalModelAdmin[BotState]):
    list_display = ["key", "value", "updated_at"]
    search_fields = ["key", "value"]
    ordering = ["key"]
    readonly_fields = ["key", "value", "updated_at"]

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False  # Managed by Discord bot

    def has_delete_permission(self, request: HttpRequest, obj: BotState | None = None) -> bool:
        return False  # Internal bot state


@admin.register(CompetitionConfig)
class CompetitionConfigAdmin(PortalModelAdmin[CompetitionConfig]):
    list_display = [
        "competition_status",
        "competition_start_time",
        "competition_end_time",
        "applications_enabled",
        "max_team_members",
    ]
    readonly_fields = ["created_at", "updated_at", "applications_enabled"]

    fieldsets = (
        (
            "Competition Timing",
            {
                "fields": (
                    "competition_start_time",
                    "competition_end_time",
                    "applications_enabled",
                ),
                "description": "Set start/end times for automatic application enable/disable. "
                "Applications will be automatically enabled at start time and disabled at end time.",
            },
        ),
        (
            "Application Control",
            {
                "fields": ("controlled_applications",),
                "description": (
                    "List of Authentik application slugs to control (e.g., ['scoring', 'quotient2', 'semaphore'])."
                    " These applications will be enabled/disabled based on competition timing."
                ),
            },
        ),
        (
            "Team Settings",
            {
                "fields": ("max_team_members",),
                "description": "Maximum number of members allowed per team.",
            },
        ),
        (
            "System Info",
            {
                "fields": ("created_at", "updated_at"),
                "description": "Audit and system information.",
            },
        ),
    )

    @admin.display(description="Status")
    def competition_status(self, obj: CompetitionConfig) -> str:
        from django.utils import timezone

        if obj.applications_enabled:
            return "Active"
        if obj.competition_start_time and timezone.now() < obj.competition_start_time:
            return "Scheduled"
        if obj.competition_end_time and timezone.now() > obj.competition_end_time:
            return "Ended"
        return "Not Scheduled"

    def has_add_permission(self, request: HttpRequest) -> bool:
        return not CompetitionConfig.objects.exists()

    def has_delete_permission(self, request: HttpRequest, obj: CompetitionConfig | None = None) -> bool:
        return False  # Singleton - never delete


@admin.register(UserGroups)
class UserGroupsAdmin(PortalModelAdmin[UserGroups]):
    list_display = ["user", "authentik_id", "group_count"]
    search_fields = ["user__username", "authentik_id"]
    readonly_fields = ["user", "authentik_id", "groups"]

    @admin.display(description="Groups")
    def group_count(self, obj: UserGroups) -> str:
        return str(len(obj.groups))

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False  # Created by OAuth flow

    def has_delete_permission(self, request: HttpRequest, obj: UserGroups | None = None) -> bool:
        # Deleting strips the user's permissions; their next login makes a new account and renames
        # the old one aside (with its Discord links), so prefer letting the group refresh update it.
        return True
