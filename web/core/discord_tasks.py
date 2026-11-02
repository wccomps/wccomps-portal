"""The work the portal queues for the bot: one payload dataclass per DiscordTask type.

TaskPayload is the registry. DiscordTask.task_type's choices come from it, DiscordTask.enqueue
takes one of its members, and the bot's dispatch matches on it exhaustively, so mypy rejects a
payload type the bot cannot handle.
"""

from dataclasses import dataclass
from typing import ClassVar, TypedDict, get_args


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
class TicketCreatedWeb:
    task_type: ClassVar = "ticket_created_web"
    label: ClassVar = "Ticket Created via Web"
    ticket_id: int


class SyncRolesResult(TypedDict):
    """What a role sync did: written by the bot, shown by the web Sync Roles page."""

    roles_added: int
    roles_removed: int
    errors: int
    changes: list[str]


@dataclass(frozen=True, kw_only=True)
class SyncRoles:
    """The SyncRolesResult lands in DiscordTask.result."""

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
class CleanupCompetition:
    """Tear down the competition (see bot.competition_actions.run_competition_cleanup).

    closed_out_event_id: set by Close Out Event, the event it closed. Its transaction already reset the database,
    so the bot does only the Discord side, and skips if an event was opened or the competition started since.
    Without it (rows queued before the field existed) the bot runs the full cleanup."""

    task_type: ClassVar = "cleanup_competition"
    label: ClassVar = "Clean Up Competition"
    requested_by: str
    closed_out_event_id: int | None = None


type TaskPayload = (
    PostComment
    | BroadcastMessage
    | SyncMemberRoles
    | SetupTeamInfrastructure
    | LogToChannel
    | PostTicketUpdate
    | TicketCreatedWeb
    | SyncRoles
    | AddUserToThread
    | CleanupCompetition
)

PAYLOAD_TYPES: dict[str, type[TaskPayload]] = {cls.task_type: cls for cls in get_args(TaskPayload.__value__)}
