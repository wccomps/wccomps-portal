"""Notices for the ops team: audited, then queued for every configured chat system's ops channel."""

from django.db import transaction

from core.models import AuditLog, QueuedTask
from core.queued_tasks import LogToChannel

OPS_NOTICE = "ops_notice"


def notify_ops(message: str, *, actor: str = "system") -> None:
    """Record message in the audit log, so it is kept with no chat system configured, and queue it for ops chat."""
    with transaction.atomic():
        AuditLog.objects.create(
            action=OPS_NOTICE, admin_user=actor, target_entity="ops", target_id=0, details={"message": message}
        )
        QueuedTask.enqueue(LogToChannel(message=message))
