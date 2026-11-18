"""Which Authentik groups grant each permission: the groups of the roles holding it (portal.settings.PORTAL_ROLE_GROUPS,
one setting per role)."""

from django.conf import settings

_ROLES: dict[str, tuple[str, ...]] = {
    "admin": ("admin",),
    "ticketing_admin": ("ticketing_admin", "admin"),
    "ticketing_support": ("ticketing_support", "ticketing_admin", "admin"),
    "gold_team": ("gold", "admin"),
    "white_team": ("white", "gold", "admin"),
    "orange_team": ("orange", "gold", "admin"),
    "white_team_lead": ("white_lead", "gold", "admin"),
    "orange_team_lead": ("orange_lead", "gold", "admin"),
    "red_team": ("red", "admin"),
    "blue_team": ("gold", "admin"),
}

PERMISSION_MAP: dict[str, list[str]] = {
    permission: [settings.PORTAL_ROLE_GROUPS[role] for role in roles] for permission, roles in _ROLES.items()
}

# Permissions an active team's group grants as well (TeamQuerySet.held_by), which only the database knows
TEAM_HELD = frozenset({"blue_team"})
