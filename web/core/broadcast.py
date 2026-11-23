"""Who a broadcast goes to: parsed from BroadcastMessage.target, at the portal's form and again by each chat backend,
which sends to target_teams; and queueing one, from the portal or the /competition broadcast command."""

from dataclasses import dataclass

from core.authentik_utils import parse_team_range
from core.models import AuditLog, QueuedTask
from core.queued_tasks import BroadcastMessage
from team.models import Team


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
    return Teams(tuple(parse_team_range(target)))


async def target_teams(target: AllTeams | Teams) -> tuple[list[Team], list[int]]:
    """The teams a broadcast to target reaches, by number: every active team, or the range's teams that exist, active
    or not; and the range's numbers that have no team."""
    match target:
        case AllTeams():
            return [t async for t in Team.objects.filter(is_active=True).order_by("team_number")], []
        case Teams(numbers):
            by_number = {t.team_number: t async for t in Team.objects.filter(team_number__in=numbers)}
            return [by_number[n] for n in sorted(by_number)], [n for n in numbers if n not in by_number]


def queue_broadcast(target: str, message: str, sender: str) -> None:
    """Queue the broadcast and audit it."""
    QueuedTask.enqueue(BroadcastMessage(target=target, message=message, sender=sender))
    AuditLog.objects.create(
        action="broadcast_message",
        admin_user=sender,
        target_entity="broadcast",
        target_id=0,
        details={"target": target, "message_preview": message[:200]},
    )
