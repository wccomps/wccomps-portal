"""Which Authentik groups grant each permission."""

from django.conf import settings

_PREFIX = settings.PORTAL_GROUP_PREFIX
_ADMIN = settings.PORTAL_ADMIN_GROUP

PERMISSION_MAP: dict[str, list[str]] = {
    "admin": [_ADMIN],
    "ticketing_admin": [f"{_PREFIX}Ticketing_Admin", _ADMIN],
    "ticketing_support": [f"{_PREFIX}Ticketing_Support", f"{_PREFIX}Ticketing_Admin", _ADMIN],
    "gold_team": [f"{_PREFIX}GoldTeam", _ADMIN],
    "white_team": [f"{_PREFIX}WhiteTeam", f"{_PREFIX}GoldTeam", _ADMIN],
    "orange_team": [f"{_PREFIX}OrangeTeam", f"{_PREFIX}GoldTeam", _ADMIN],
    "white_team_lead": [f"{_PREFIX}WhiteTeam_Lead", f"{_PREFIX}GoldTeam", _ADMIN],
    "orange_team_lead": [f"{_PREFIX}OrangeTeam_Lead", f"{_PREFIX}GoldTeam", _ADMIN],
    "red_team": [f"{_PREFIX}RedTeam", _ADMIN],
    "blue_team": [f"{_PREFIX}GoldTeam", _ADMIN],
}

# Permissions an active team's group grants as well (TeamQuerySet.held_by), which only the database knows
TEAM_HELD = frozenset({"blue_team"})
