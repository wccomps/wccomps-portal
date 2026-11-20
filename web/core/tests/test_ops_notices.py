"""notify_ops keeps every ops notice in the audit log and queues it for each configured chat system."""

import pytest

from core.models import AuditLog, QueuedTask
from core.queued_tasks import LogToChannel
from core.services.ops import notify_ops

pytestmark = pytest.mark.django_db


def test_notify_ops_audits_without_chat(settings):
    settings.DISCORD_BOT_TOKEN = ""
    notify_ops("Competition Auto-Start Failed: x")
    assert AuditLog.objects.get(action="ops_notice").details == {"message": "Competition Auto-Start Failed: x"}
    assert not QueuedTask.objects.exists()


def test_notify_ops_queues_with_chat():
    notify_ops("m")
    assert QueuedTask.objects.get().typed_payload() == LogToChannel(message="m")


def test_notify_ops_queues_for_every_chat_backend(zulip_configured):
    notify_ops("m", actor="timer")
    assert sorted(QueuedTask.objects.values_list("backend", flat=True)) == ["discord", "zulip"]
    log = AuditLog.objects.get(action="ops_notice")
    assert (log.admin_user, log.target_entity, log.target_id) == ("timer", "ops", 0)
