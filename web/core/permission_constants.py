"""Which Authentik groups grant each permission."""

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
    "blue_team": ["WCComps_GoldTeam", "WCComps_Discord_Admin"],
}

# Permissions an active team's group grants as well (TeamQuerySet.held_by), which only the database knows
TEAM_HELD = frozenset({"blue_team"})
