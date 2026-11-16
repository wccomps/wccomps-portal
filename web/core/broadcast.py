"""Who a broadcast goes to: parsed from BroadcastMessage.target, at the portal's form and again by the bot; and
queueing one, from the portal or the /competition broadcast command."""

from dataclasses import dataclass

from core.authentik_utils import parse_team_range
from core.models import AuditLog, QueuedTask
from core.queued_tasks import BroadcastMessage


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
