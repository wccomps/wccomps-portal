"""Authorization via Authentik groups."""

from collections.abc import Callable
from typing import Concatenate, ParamSpec

from django.contrib.auth.models import AnonymousUser, User
from django.core.exceptions import PermissionDenied
from django.db import connection
from django.db.models import Model, QuerySet
from django.http import HttpRequest, HttpResponseBase
from django.shortcuts import get_object_or_404

from team.models import Team

from .models import UserGroups
from .permission_constants import PERMISSION_MAP as PERMISSION_MAP
from .permission_constants import TEAM_HELD

P = ParamSpec("P")
type ViewFunc[**P] = Callable[Concatenate[HttpRequest, P], HttpResponseBase]


def permission_groups_of(user: User | AnonymousUser) -> list[str]:
    """The user's permission groups (UserGroups.permission_groups): none signed out, with no row, or while the account
    is disabled."""
    if isinstance(user, AnonymousUser):
        return []

    try:
        return user.usergroups.permission_groups
    except UserGroups.DoesNotExist:
        return []


def get_authentik_id(user: User) -> str | None:
    try:
        return user.usergroups.authentik_id
    except UserGroups.DoesNotExist:
        return None


def get_permissions_context(user: User) -> dict[str, bool]:
    return {f"is_{perm}": has_permission(user, perm) for perm in PERMISSION_MAP}


def groups_grant(groups: list[str], permission_name: str) -> bool:
    """Whether these Authentik groups grant the permission: a group PERMISSION_MAP names, or for a TEAM_HELD one an
    active team's group. ValueError for a name that isn't a permission."""
    if permission_name not in PERMISSION_MAP:
        raise ValueError(f"No permission named {permission_name!r}")
    if any(group in groups for group in PERMISSION_MAP[permission_name]):
        return True
    return permission_name in TEAM_HELD and team_for_groups(groups) is not None


def users_granted(permission_name: str) -> QuerySet[User]:
    """The users groups_grant passes, as one query: an enabled account holding one of the permission's groups.
    ValueError for a TEAM_HELD permission, which a team's group grants as well."""
    if permission_name in TEAM_HELD:
        raise ValueError(f"{permission_name} is team-held; users_granted lists only group-granted permissions")
    return User.objects.filter(usergroups__in=UserGroups.objects.granting(PERMISSION_MAP[permission_name]))


def lock_account(authentik_id: str) -> None:
    """Hold the account's lock until the transaction ends, so a sign-in and a credentials reset's sign-out of the
    same account run one after the other."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", [f"authentik:{authentik_id}"])


def sign_out_everywhere(user: User) -> None:
    """End every portal session of the user: a new session auth hash fails each session's own (signed_out_since)."""
    user.set_unusable_password()
    user.save(update_fields=["password"])


def signed_out_since(user: User) -> bool:
    """Whether sign_out_everywhere ran after this in-memory user was loaded, ending the session it came with."""
    return User.objects.get(pk=user.pk).get_session_auth_hash() != user.get_session_auth_hash()


def has_permission(user: User | AnonymousUser, permission_name: str) -> bool:
    return groups_grant(permission_groups_of(user), permission_name)


def uses_django_admin(user: User | AnonymousUser) -> bool:
    """Who gets into the Django admin and holds Django's model permissions: an active user with the admin permission."""
    return user.is_active and has_permission(user, "admin")


def team_for_groups(groups: list[str]) -> Team | None:
    """The team these groups put the account on (TeamQuerySet.held_by), or None."""
    return Team.objects.held_by(groups).first()


def get_user_team(user: User | AnonymousUser) -> Team | None:
    return team_for_groups(permission_groups_of(user))


def get_role_based_landing_url(groups: list[str]) -> str:
    """Determine the landing page URL based on a user's Authentik groups, checking roles in priority order."""
    from django.urls import reverse

    if groups_grant(groups, "ticketing_support"):
        return reverse("ticket_list")
    if groups_grant(groups, "white_team"):
        return reverse("leaderboard_page")
    if groups_grant(groups, "red_team"):
        return reverse("scoring:submit_red_score")
    if groups_grant(groups, "orange_team"):
        return reverse("orange_team:dashboard")
    if groups_grant(groups, "blue_team"):
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
