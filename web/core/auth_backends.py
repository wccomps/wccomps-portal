"""Sessions and Django's own permissions for the portal; signing in is Authentik's (core.oauth)."""

from asgiref.sync import sync_to_async
from django.contrib.auth.backends import BaseBackend
from django.contrib.auth.models import AnonymousUser, User
from django.db.models import Model

from .auth_utils import uses_django_admin


class PortalBackend(BaseBackend):
    """Finds a signed-in session's user, and gives Django's model permissions (the Django admin's model pages check
    user.has_perm) only to a user the admin site lets in, every one, sync and async. It authenticates no one, so
    nothing signs in with a password. User.has_perm still answers True for an active superuser before asking any
    backend, until production's superuser flags are cleared; the admin site's gate keeps such a user out.
    """

    def get_user(self, user_id: int) -> User | None:
        return User.objects.filter(pk=user_id, is_active=True).first()

    async def aget_user(self, user_id: int) -> User | None:
        return await User.objects.filter(pk=user_id, is_active=True).afirst()

    def has_perm(self, user_obj: User | AnonymousUser, perm: str, obj: Model | None = None) -> bool:
        return uses_django_admin(user_obj)

    def has_module_perms(self, user_obj: User | AnonymousUser, app_label: str) -> bool:
        return uses_django_admin(user_obj)

    async def ahas_perm(self, user_obj: User | AnonymousUser, perm: str, obj: Model | None = None) -> bool:
        return await sync_to_async(uses_django_admin)(user_obj)

    async def ahas_module_perms(self, user_obj: User | AnonymousUser, app_label: str) -> bool:
        return await sync_to_async(uses_django_admin)(user_obj)
