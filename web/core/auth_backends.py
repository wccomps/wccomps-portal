"""Django's own permissions from Authentik groups."""

from asgiref.sync import sync_to_async
from django.contrib.auth.backends import ModelBackend
from django.contrib.auth.models import AnonymousUser, User
from django.db.models import Model

from .auth_utils import uses_django_admin


class PortalBackend(ModelBackend):
    """ModelBackend's sessions, with Django's model permissions only from the admin permission (PERMISSION_MAP).

    The Django admin's model pages check Django's own permissions (user.has_perm): every one, sync and async, for a
    user the admin site lets in, and none from is_superuser, Permission rows or Django groups. User.has_perm still
    answers True for an active superuser before asking any backend; the admin site's gate, the same rule, keeps one
    without the admin permission out.
    """

    def _get_permissions(self, user_obj: User | AnonymousUser, obj: Model | None, from_name: str) -> set[str]:
        return set()

    async def _aget_permissions(self, user_obj: User | AnonymousUser, obj: Model | None, from_name: str) -> set[str]:
        return set()

    def has_perm(self, user_obj: User | AnonymousUser, perm: str, obj: Model | None = None) -> bool:
        return uses_django_admin(user_obj)

    def has_module_perms(self, user_obj: User | AnonymousUser, app_label: str) -> bool:
        return uses_django_admin(user_obj)

    async def ahas_perm(self, user_obj: User | AnonymousUser, perm: str, obj: Model | None = None) -> bool:
        return await sync_to_async(uses_django_admin)(user_obj)

    async def ahas_module_perms(self, user_obj: User | AnonymousUser, app_label: str) -> bool:
        return await sync_to_async(uses_django_admin)(user_obj)
