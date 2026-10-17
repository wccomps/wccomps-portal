"""Sessions and Django's own permissions for the portal; signing in is Authentik's (core.oauth)."""

from asgiref.sync import sync_to_async
from django.contrib.auth.backends import BaseBackend
from django.contrib.auth.models import AnonymousUser, User
from django.db.models import Model

from .auth_utils import uses_django_admin


class PortalBackend(BaseBackend):
    """A session's user, and every Django model permission (has_perm and has_module_perms, sync and async) for a
    user the admin site lets in, none for anyone else; get_all_permissions lists none. It authenticates no one, so
    nothing signs in with a password. User.has_perm passes an active superuser before asking any backend, so a user
    whose superuser flag is set still passes it; the admin site's gate keeps such a user out.
    """

    def get_user(self, user_id: int) -> User | None:
        return User.objects.filter(pk=user_id, is_active=True).first()

    def has_perm(self, user_obj: User | AnonymousUser, perm: str, obj: Model | None = None) -> bool:
        return uses_django_admin(user_obj)

    def has_module_perms(self, user_obj: User | AnonymousUser, app_label: str) -> bool:
        return uses_django_admin(user_obj)

    async def ahas_perm(self, user_obj: User | AnonymousUser, perm: str, obj: Model | None = None) -> bool:
        return await sync_to_async(uses_django_admin)(user_obj)

    async def ahas_module_perms(self, user_obj: User | AnonymousUser, app_label: str) -> bool:
        return await sync_to_async(uses_django_admin)(user_obj)
