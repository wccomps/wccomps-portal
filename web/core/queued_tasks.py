"""The work the portal queues: one payload dataclass per QueuedTask type.

TaskPayload is the registry. QueuedTask.task_type's choices come from it, QueuedTask.enqueue
takes one of its members and writes one row per backend from backends_for, and the bot's
dispatch matches on it exhaustively, so mypy rejects a payload type the bot cannot handle.
"""

from dataclasses import dataclass
from typing import ClassVar, TypedDict, get_args

from core.task_backends import TaskBackend, chat_backends, discord_enabled


class PermanentTaskError(Exception):
    """Queued work that retrying can't fix: a queue that honours it fails the task at once."""


@dataclass(frozen=True, kw_only=True)
class PostComment:
    task_type: ClassVar = "post_comment"
    label: ClassVar = "Post Comment to Thread"
    ticket_id: int
    comment_id: int


@dataclass(frozen=True, kw_only=True)
class BroadcastMessage:
    task_type: ClassVar = "broadcast_message"
    label: ClassVar = "Broadcast Message"
    target: str
    message: str
    sender: str


@dataclass(frozen=True, kw_only=True)
class SyncMemberRoles:
    """Bring one member's synced roles in line with their links, e.g. right after they link or unlink."""

    task_type: ClassVar = "sync_member_roles"
    label: ClassVar = "Sync a Member's Roles"
    discord_id: int


@dataclass(frozen=True, kw_only=True)
class SetupTeamInfrastructure:
    task_type: ClassVar = "setup_team_infrastructure"
    label: ClassVar = "Setup Team Infrastructure"
    team_number: int
    recreate: bool = False


@dataclass(frozen=True, kw_only=True)
class LogToChannel:
    task_type: ClassVar = "log_to_channel"
    label: ClassVar = "Log to Ops Channel"
    message: str


@dataclass(frozen=True, kw_only=True)
class PostTicketUpdate:
    """A ticket status change (claimed, resolved, reopened, ...) for its Discord thread and the dashboard."""

    task_type: ClassVar = "post_ticket_update"
    label: ClassVar = "Post Ticket Update to Thread"
    ticket_id: int
    action: str
    actor: str
    assignee: str = ""
    resolution_notes: str = ""
    points_charged: int = 0
    reason: str = ""


@dataclass(frozen=True, kw_only=True)
class TicketCreated:
    """A new ticket, from the web or from chat. task_type keeps its old name so rows queued before the rename parse."""

    task_type: ClassVar = "ticket_created_web"
    label: ClassVar = "Ticket Created"
    ticket_id: int


@dataclass(frozen=True, kw_only=True)
class CompetitionStatusChanged:
    """The competition started, stopped or changed its schedule: chat status displays refresh."""

    task_type: ClassVar = "competition_status_changed"
    label: ClassVar = "Competition Status Changed"


class SyncRolesResult(TypedDict):
    """What a role sync did: written by the bot, shown by the web Sync Roles page."""

    roles_added: int
    roles_removed: int
    errors: int
    changes: list[str]


@dataclass(frozen=True, kw_only=True)
class SyncRoles:
    """The SyncRolesResult lands in QueuedTask.result."""

    task_type: ClassVar = "sync_roles"
    label: ClassVar = "Sync Roles from Authentik Groups"
    requested_by: str
    dry_run: bool


@dataclass(frozen=True, kw_only=True)
class AddUserToThread:
    """thread_id is unused; it stays one release so a bot on the previous release can decode new rows."""

    task_type: ClassVar = "add_user_to_thread"
    label: ClassVar = "Add User to Thread"
    ticket_id: int
    discord_id: int
    thread_id: int | None = None


@dataclass(frozen=True, kw_only=True)
class ReconcileTeams:
    """Tear every team's channels and role down while the open event is still open_event_id (None, none open), and
    set the teams with linked members up again, whatever became of the teardown."""

    task_type: ClassVar = "reconcile_teams"
    label: ClassVar = "Reconcile Team Channels"
    requested_by: str
    open_event_id: int | None = None


@dataclass(frozen=True, kw_only=True)
class ClearTeamChannels:
    """Delete the Discord role and category of a team marked channels_need_clearing, which unsets the mark only when
    nothing is left and then queues the team's setup if members linked meanwhile."""

    task_type: ClassVar = "clear_team_channels"
    label: ClassVar = "Clear a Team's Old Channels"
    team_number: int


# Every configured chat backend gets its own row of these
type ChatNotice = (
    PostComment | BroadcastMessage | LogToChannel | PostTicketUpdate | TicketCreated | CompetitionStatusChanged
)

# Only the Discord backend handles these, and only when Discord is configured
type DiscordOnly = (
    SyncMemberRoles | SetupTeamInfrastructure | SyncRoles | AddUserToThread | ReconcileTeams | ClearTeamChannels
)

type TaskPayload = ChatNotice | DiscordOnly

_CHAT_NOTICES: tuple[type[ChatNotice], ...] = get_args(ChatNotice.__value__)
_DISCORD_ONLY: tuple[type[DiscordOnly], ...] = get_args(DiscordOnly.__value__)

PAYLOAD_TYPES: dict[str, type[TaskPayload]] = {cls.task_type: cls for cls in _CHAT_NOTICES + _DISCORD_ONLY}


def backends_for(payload: TaskPayload) -> list[TaskBackend]:
    """The backends that get a row of this payload, from its group."""
    if isinstance(payload, _CHAT_NOTICES):
        return chat_backends()
    return [TaskBackend.DISCORD] if discord_enabled() else []
