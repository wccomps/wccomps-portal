"""QueuedTask.enqueue writes one row per backend that handles the payload."""

import pytest

from core.models import QueuedTask
from core.queued_tasks import LogToChannel, PostComment, ResetNextcloudTeam, SyncRoles
from core.task_backends import TaskBackend
from team.models import Team
from ticketing.models import Ticket, TicketCategory, TicketComment

pytestmark = pytest.mark.django_db


@pytest.fixture
def ticket(blue_team: Team) -> Ticket:
    return Ticket.objects.create(
        ticket_number="T001-001",
        team=blue_team,
        category=TicketCategory.objects.get(pk=6),
        title="Queue test",
        description="Queue test",
    )


@pytest.fixture
def comment(ticket: Ticket, blue_team_user) -> TicketComment:
    return TicketComment.objects.create(ticket=ticket, author=blue_team_user, comment_text="Hello")


def test_enqueue_writes_nothing_without_chat(settings):
    settings.DISCORD_BOT_TOKEN = ""
    assert QueuedTask.enqueue(LogToChannel(message="x")) == []
    assert not QueuedTask.objects.exists()


def test_notice_one_row_per_chat_backend(zulip_configured, ticket, comment):
    rows = QueuedTask.enqueue(PostComment(ticket_id=ticket.id, comment_id=comment.id))
    assert sorted(r.backend for r in rows) == ["discord", "zulip"]


def test_discord_only_payload_skips_zulip(zulip_configured):
    assert [r.backend for r in QueuedTask.enqueue(SyncRoles(requested_by="a", dry_run=True))] == ["discord"]


def test_discord_only_payload_writes_nothing_without_discord(zulip_configured, settings):
    settings.DISCORD_BOT_TOKEN = ""
    assert QueuedTask.enqueue(SyncRoles(requested_by="a", dry_run=True)) == []


def test_worker_task_queued_without_chat(settings):
    settings.DISCORD_BOT_TOKEN = ""
    assert [r.backend for r in QueuedTask.enqueue(ResetNextcloudTeam(team_number=3))] == ["worker"]


def test_origin_is_skipped(zulip_configured, ticket, comment):
    rows = QueuedTask.enqueue(PostComment(ticket_id=ticket.id, comment_id=comment.id), origin=TaskBackend.DISCORD)
    assert [r.backend for r in rows] == ["zulip"]
