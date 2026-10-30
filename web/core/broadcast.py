"""Who a broadcast goes to: parsed from BroadcastMessage.target, at the portal's form and again by the bot."""

from dataclasses import dataclass

from core.authentik_utils import parse_team_range


@dataclass(frozen=True)
class Announcements:
    pass


@dataclass(frozen=True)
class AllTeams:
    pass


@dataclass(frozen=True)
class Teams:
    numbers: tuple[int, ...]


type BroadcastTarget = Announcements | AllTeams | Teams


def parse_broadcast_target(target: str) -> BroadcastTarget:
    """ "announcements", "all-teams" or a team range like "1,3,5-10"; raises ValueError for a bad range or one naming
    no team."""
    target = target.lower().strip()
    if target == "announcements":
        return Announcements()
    if target == "all-teams":
        return AllTeams()
    numbers = parse_team_range(target)
    if not numbers:
        raise ValueError("The target names no team")
    return Teams(tuple(numbers))
