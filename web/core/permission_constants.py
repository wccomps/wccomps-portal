"""Which Authentik groups grant each permission: each role's group is its own setting (portal.settings)."""

from django.conf import settings

_ADMIN = settings.PORTAL_ADMIN_GROUP
_GOLD = settings.PORTAL_GOLD_GROUP
_TICKETING_ADMIN = settings.PORTAL_TICKETING_ADMIN_GROUP

PERMISSION_MAP: dict[str, list[str]] = {
    "admin": [_ADMIN],
    "ticketing_admin": [_TICKETING_ADMIN, _ADMIN],
    "ticketing_support": [settings.PORTAL_TICKETING_SUPPORT_GROUP, _TICKETING_ADMIN, _ADMIN],
    "gold_team": [_GOLD, _ADMIN],
    "white_team": [settings.PORTAL_WHITE_GROUP, _GOLD, _ADMIN],
    "orange_team": [settings.PORTAL_ORANGE_GROUP, _GOLD, _ADMIN],
    "white_team_lead": [settings.PORTAL_WHITE_LEAD_GROUP, _GOLD, _ADMIN],
    "orange_team_lead": [settings.PORTAL_ORANGE_LEAD_GROUP, _GOLD, _ADMIN],
    "red_team": [settings.PORTAL_RED_GROUP, _ADMIN],
    "blue_team": [_GOLD, _ADMIN],
}

# Permissions an active team's group grants as well (TeamQuerySet.held_by), which only the database knows
TEAM_HELD = frozenset({"blue_team"})
