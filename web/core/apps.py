from django.apps import AppConfig
from django.contrib.admin.apps import AdminConfig


class PortalAdminConfig(AdminConfig):
    default_site = "core.admin.AuthentikAdminSite"

    def ready(self) -> None:
        from django.contrib import admin
        from django.contrib.auth.models import Group, User

        # Import core.admin before autodiscover so DefaultAdminSite._setup resolves AuthentikAdminSite from sys.modules
        # instead of importing core.admin mid-setup while @admin.register runs.
        import core.admin  # noqa: F401

        super().ready()
        # Accounts and groups live in Authentik (UserGroups shows the cached memberships); Django's User and Group
        # admins would offer password forms, superuser/staff flags and the unused auth_group table.
        admin.site.unregister([User, Group])


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"

    def ready(self) -> None:
        from core.cotton import escape_component_attrs

        escape_component_attrs()
