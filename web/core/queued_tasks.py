"""The work the portal queues: one payload dataclass per QueuedTask type.

TaskPayload is the registry. QueuedTask.task_type's choices come from it, QueuedTask.enqueue
takes one of its members and writes one row per configured backend whose groups (ChatNotice,
DiscordOnly, WorkerTask) BACKEND_TASKS says include it. Each backend's processor
(core.task_queue.QueueProcessor) is generic over the groups BACKEND_TASKS gives that backend and
matches on them exhaustively, so mypy rejects a member of those groups it can't handle; a row of
any other type fails at once.
"""

from dataclasses import dataclass
from typing import ClassVar, TypedDict, get_args

from core.task_backends import TaskBackend, discord_enabled, zulip_enabled


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


@dataclass(frozen=True, kw_only=True)
class ReconcileNextcloud:
    """Delete every team's Nextcloud account while the open event is still open_event_id (None, none open), asking
    again before each team, then provision the active teams' accounts for an open event when nothing stopped it."""

    task_type: ClassVar = "reconcile_nextcloud"
    label: ClassVar = "Reconcile Nextcloud Accounts"
    requested_by: str
    open_event_id: int | None = None


@dataclass(frozen=True, kw_only=True)
class ResetNextcloudTeam:
    """Delete a team's Nextcloud account and files, and provision it again when the team is active: the team was
    handed to a new school."""

    task_type: ClassVar = "reset_nextcloud_team"
    label: ClassVar = "Reset a Team's Nextcloud Account"
    team_number: int


@dataclass(frozen=True, kw_only=True)
class SyncQuotientMetadata:
    """Sync the Quotient metadata (Proxmox IPs, aweful-proxy config) as requested_by; queued by a start so the chat
    process needs no Proxmox or aweful-proxy credentials."""

    task_type: ClassVar = "sync_quotient_metadata"
    label: ClassVar = "Sync Quotient Metadata"
    requested_by: str


# Every configured chat backend gets its own row of these
type ChatNotice = (
    PostComment | BroadcastMessage | LogToChannel | PostTicketUpdate | TicketCreated | CompetitionStatusChanged
)

# Only the Discord backend handles these, and only when Discord is configured
type DiscordOnly = (
    SyncMemberRoles | SetupTeamInfrastructure | SyncRoles | AddUserToThread | ReconcileTeams | ClearTeamChannels
)

# The worker handles these, whatever chat systems are configured
type WorkerTask = ReconcileNextcloud | ResetNextcloudTeam | SyncQuotientMetadata

type TaskPayload = ChatNotice | DiscordOnly | WorkerTask

CHAT_NOTICES: tuple[type[ChatNotice], ...] = get_args(ChatNotice.__value__)
DISCORD_ONLY: tuple[type[DiscordOnly], ...] = get_args(DiscordOnly.__value__)
WORKER_TASKS: tuple[type[WorkerTask], ...] = get_args(WorkerTask.__value__)

PAYLOAD_TYPES: dict[str, type[TaskPayload]] = {cls.task_type: cls for cls in CHAT_NOTICES + DISCORD_ONLY + WORKER_TASKS}


# The payload groups each backend runs
BACKEND_TASKS: dict[TaskBackend, tuple[type[TaskPayload], ...]] = {
    TaskBackend.DISCORD: CHAT_NOTICES + DISCORD_ONLY,
    TaskBackend.ZULIP: CHAT_NOTICES,
    TaskBackend.WORKER: WORKER_TASKS,
}


def backends_for(payload: TaskPayload) -> list[TaskBackend]:
    """The configured backends that run this payload's group."""
    configured = {TaskBackend.DISCORD: discord_enabled(), TaskBackend.ZULIP: zulip_enabled(), TaskBackend.WORKER: True}
    return [backend for backend, types in BACKEND_TASKS.items() if configured[backend] and isinstance(payload, types)]
