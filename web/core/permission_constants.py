"""Permission constants and group-checking logic shared by web and bot."""

import re

BLUETEAM_GROUP_PATTERN = re.compile(r"^WCComps_BlueTeam(\d+)$")


PERMISSION_MAP: dict[str, list[str]] = {
    "admin": ["WCComps_Discord_Admin"],
    "ticketing_admin": ["WCComps_Ticketing_Admin", "WCComps_Discord_Admin"],
    "ticketing_support": ["WCComps_Ticketing_Support", "WCComps_Ticketing_Admin", "WCComps_Discord_Admin"],
    "gold_team": ["WCComps_GoldTeam", "WCComps_Discord_Admin"],
    "white_team": ["WCComps_WhiteTeam", "WCComps_GoldTeam", "WCComps_Discord_Admin"],
    "orange_team": ["WCComps_OrangeTeam", "WCComps_GoldTeam", "WCComps_Discord_Admin"],
    "white_team_lead": ["WCComps_WhiteTeam_Lead", "WCComps_GoldTeam", "WCComps_Discord_Admin"],
    "orange_team_lead": ["WCComps_OrangeTeam_Lead", "WCComps_GoldTeam", "WCComps_Discord_Admin"],
    "red_team": ["WCComps_RedTeam", "WCComps_Discord_Admin"],
    "helper_eligible": ["WCComps_Ticketing_Support", "WCComps_Quotient_Injects", "WCComps_Discord_Admin"],
}


def check_groups_for_permission(groups: list[str], permission_name: str) -> bool:
    """Check if Authentik groups grant a permission; a name not in PERMISSION_MAP must match a group exactly."""
    if permission_name == "blue_team":
        return any(
            BLUETEAM_GROUP_PATTERN.match(g) or g in ("WCComps_GoldTeam", "WCComps_Discord_Admin") for g in groups
        )

    if permission_name in PERMISSION_MAP:
        return any(g in groups for g in PERMISSION_MAP[permission_name])

    return permission_name in groups
