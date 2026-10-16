"""Django model permissions from Authentik groups."""

from asgiref.sync import sync_to_async
from django.contrib.auth.backends import BaseBackend
from django.contrib.auth.models import AnonymousUser, User
from django.db.models import Model

from . import auth_utils


class AuthentikAdminBackend(BaseBackend):
    """Every Django model permission, in sync and async checks, for a user with the admin permission (PERMISSION_MAP);
    nothing from this backend for anyone else.

    The Django admin's model pages check Django's own permissions (user.has_perm), which the portal grants no one:
    who is an admin is an Authentik group. Superusers and ModelBackend's Permission rows still grant permissions, but
    only behind the admin site's gate (AuthentikAdminSite.has_permission). Signing in is the OAuth flow's, so this
    backend authenticates no one.
    """

    def has_perm(self, user_obj: User | AnonymousUser, perm: str, obj: Model | None = None) -> bool:
        return self._is_admin(user_obj)

    def has_module_perms(self, user_obj: User | AnonymousUser, app_label: str) -> bool:
        return self._is_admin(user_obj)

    async def ahas_perm(self, user_obj: User | AnonymousUser, perm: str, obj: Model | None = None) -> bool:
        return await sync_to_async(self._is_admin)(user_obj)

    async def ahas_module_perms(self, user_obj: User | AnonymousUser, app_label: str) -> bool:
        return await sync_to_async(self._is_admin)(user_obj)

    @staticmethod
    def _is_admin(user_obj: User | AnonymousUser) -> bool:
        # Looked up through the module at each check, so a test's patch of has_permission is seen whenever it applies
        return user_obj.is_active and auth_utils.has_permission(user_obj, "admin")
