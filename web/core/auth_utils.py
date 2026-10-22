"""Authorization via Authentik groups."""

from collections.abc import Callable
from typing import Concatenate, ParamSpec

from django.contrib.auth.models import AnonymousUser, User
from django.core.exceptions import PermissionDenied
from django.db.models import Model, QuerySet
from django.http import HttpRequest, HttpResponseBase
from django.shortcuts import get_object_or_404

from team.models import Team

from .models import UserGroups
from .permission_constants import PERMISSION_MAP as PERMISSION_MAP
from .permission_constants import check_groups_for_permission as check_groups_for_permission

P = ParamSpec("P")
type ViewFunc[**P] = Callable[Concatenate[HttpRequest, P], HttpResponseBase]


def get_authentik_groups(user: User | AnonymousUser) -> list[str]:
    """Get user's Authentik groups from UserGroups, the single source of truth for permissions."""
    if isinstance(user, AnonymousUser):
        return []

    try:
        return list(user.usergroups.groups)
    except UserGroups.DoesNotExist:
        return []


def get_authentik_id(user: User) -> str | None:
    try:
        return user.usergroups.authentik_id
    except UserGroups.DoesNotExist:
        return None


def get_permissions_context(user: User) -> dict[str, bool]:
    return {f"is_{perm}": has_permission(user, perm) for perm in PERMISSION_MAP}


def has_permission(user: User | AnonymousUser, permission_name: str) -> bool:
    groups = get_authentik_groups(user)
    return check_groups_for_permission(groups, permission_name)


def uses_django_admin(user: User | AnonymousUser) -> bool:
    """Who gets into the Django admin and holds Django's model permissions: an active user with the admin permission."""
    return user.is_active and has_permission(user, "admin")


def team_for_groups(groups: list[str]) -> Team | None:
    """The active team whose Authentik group (Team.authentik_group) is among these groups, or None."""
    return Team.objects.filter(authentik_group__in=groups, is_active=True).first()


def get_user_team(user: User | AnonymousUser) -> Team | None:
    return team_for_groups(get_authentik_groups(user))


def get_role_based_landing_url(groups: list[str]) -> str:
    """Determine the landing page URL based on a user's Authentik groups, checking roles in priority order."""
    from django.urls import reverse

    if check_groups_for_permission(groups, "ticketing_support"):
        return reverse("ticket_list")
    if check_groups_for_permission(groups, "white_team"):
        return reverse("leaderboard_page")
    if check_groups_for_permission(groups, "red_team"):
        return reverse("scoring:submit_red_score")
    if check_groups_for_permission(groups, "orange_team"):
        return reverse("orange_team:dashboard")
    if check_groups_for_permission(groups, "blue_team"):
        return reverse("ticket_list")

    return "/"


def require_permission(
    *permission_names: str,
    error_message: str = "",
) -> Callable[[ViewFunc[P]], ViewFunc[P]]:
    """Decorator granting access to a view if the user has ANY of the listed permissions. A signed-out visitor is sent
    to sign in; a signed-in one without them gets the 403 (core.errors), saying error_message if it's given."""
    from functools import wraps

    from .errors import sign_in_answer

    def decorator(view_func: ViewFunc[P]) -> ViewFunc[P]:
        @wraps(view_func)
        def wrapped(request: HttpRequest, *args: P.args, **kwargs: P.kwargs) -> HttpResponseBase:
            if not request.user.is_authenticated:
                return sign_in_answer(request)
            if not any(has_permission(request.user, perm) for perm in permission_names):
                raise PermissionDenied(error_message)
            return view_func(request, *args, **kwargs)

        return wrapped  # type: ignore[return-value]

    return decorator


def team_record_or_404[M: Model](
    queryset: QuerySet[M],
    user: User | AnonymousUser,
    *,
    staff: tuple[str, ...],
    team_field: str = "team",
    **lookup: object,
) -> M:
    """A record a team owns, as the person may see it: staff (any of these permissions) see every team's, a team
    member their own team's. Another team's record answers 404 as a missing one does, so a team can't tell which
    records exist; someone neither staff nor on a team gets the 403."""
    if not any(has_permission(user, perm) for perm in staff):
        team = get_user_team(user)
        if team is None:
            raise PermissionDenied
        queryset = queryset.filter(**{team_field: team})
    return get_object_or_404(queryset, **lookup)
