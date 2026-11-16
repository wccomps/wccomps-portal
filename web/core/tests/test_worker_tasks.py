"""The worker's Nextcloud tasks: the team accounts a ReconcileNextcloud or ResetNextcloudTeam deletes and provisions."""

from dataclasses import asdict
from unittest.mock import MagicMock, patch

import pytest

from core.models import AuditLog, QueuedTask
from core.queued_tasks import ReconcileNextcloud, ResetNextcloudTeam
from core.task_queue import TaskProgress
from core.worker_tasks import WorkerQueueProcessor
from team.models import Team

# The async ORM runs on another thread's connection, which sees only committed rows
pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def teams() -> None:
    for number in range(1, 6):
        Team.objects.create(team_number=number, team_name=f"Team {number}", is_active=number <= 3)


@pytest.fixture
def nextcloud():
    manager = MagicMock()
    manager.__enter__.return_value = manager
    manager.delete_team_user.return_value = True
    with (
        patch("core.worker_tasks.NextcloudManager", return_value=manager),
        patch("core.worker_tasks.nextcloud_configured", return_value=True),
        patch("core.worker_tasks.teardown_problem", return_value="") as problem,
    ):
        manager.problem = problem
        yield manager


async def run(payload):
    """Run payload's handler as the worker does, given its claimed row's progress."""
    task = await QueuedTask.objects.acreate(
        task_type=payload.task_type, backend="worker", payload=asdict(payload), status="processing"
    )
    return await WorkerQueueProcessor().dispatch(payload, TaskProgress.of(task))


async def test_reconcile_nextcloud_tears_down_then_provisions(nextcloud):
    await run(ReconcileNextcloud(requested_by="gold", open_event_id=7))
    assert [c.args[0] for c in nextcloud.delete_team_user.call_args_list] == [1, 2, 3, 4, 5]
    assert [c.args[0] for c in nextcloud.provision_team_user.call_args_list] == [1, 2, 3]
    audit = await AuditLog.objects.aget(action="nextcloud_reconciled")
    assert (audit.details["deleted"], audit.details["provisioned"], audit.details["stopped"]) == (5, 3, "")
    summary = await QueuedTask.objects.aget(task_type="log_to_channel")
    assert summary.payload["message"] == "Nextcloud: deleted 5 team accounts, provisioned 3"


async def test_close_out_tears_down_without_provisioning(nextcloud):
    await run(ReconcileNextcloud(requested_by="gold"))
    assert nextcloud.delete_team_user.call_count == 5
    nextcloud.provision_team_user.assert_not_called()


async def test_reconcile_nextcloud_stops_when_the_event_reopens(nextcloud):  # Review Focus 7
    nextcloud.problem.side_effect = ["", "", "", "the event reopened"]  # asked once up front, then per team
    await run(ReconcileNextcloud(requested_by="gold", open_event_id=7))
    assert [c.args[0] for c in nextcloud.delete_team_user.call_args_list] == [1, 2]
    nextcloud.provision_team_user.assert_not_called()
    audit = await AuditLog.objects.aget(action="nextcloud_reconciled")
    assert audit.details["stopped"] == "the event reopened"


async def test_reconcile_nextcloud_refused_up_front_touches_nothing(nextcloud):
    nextcloud.problem.return_value = "The event it was queued for is no longer open."
    await run(ReconcileNextcloud(requested_by="gold", open_event_id=7))
    nextcloud.__enter__.assert_not_called()
    assert not await AuditLog.objects.filter(action="nextcloud_reconciled").aexists()


async def test_nothing_deleted_or_provisioned_posts_no_summary(nextcloud):
    nextcloud.delete_team_user.return_value = False
    await run(ReconcileNextcloud(requested_by="gold"))
    assert await AuditLog.objects.filter(action="nextcloud_reconciled").aexists()
    assert not await QueuedTask.objects.filter(task_type="log_to_channel").aexists()


async def test_a_failure_is_audited_and_raised_for_a_retry(nextcloud):
    from core.nextcloud import NextcloudAPIError

    nextcloud.delete_team_user.side_effect = [True, NextcloudAPIError("HTTP 502")]
    with pytest.raises(RuntimeError, match="deleted 1 team accounts.*failed partway: HTTP 502") as raised:
        await run(ReconcileNextcloud(requested_by="gold", open_event_id=7))
    assert isinstance(raised.value.__cause__, NextcloudAPIError)
    audit = await AuditLog.objects.aget(action="nextcloud_reconciled")
    assert (audit.details["deleted"], audit.details["finished"]) == (1, False)
    assert not await QueuedTask.objects.filter(task_type="log_to_channel").aexists()


async def test_a_retry_resumes_after_the_last_team_and_reports_once(nextcloud):
    from core.nextcloud import NextcloudAPIError

    nextcloud.delete_team_user.side_effect = [True, True, NextcloudAPIError("HTTP 502"), True, True, True]
    payload = ReconcileNextcloud(requested_by="gold", open_event_id=7)
    row = await QueuedTask.objects.acreate(task_type=payload.task_type, backend="worker", payload=asdict(payload))
    processor = WorkerQueueProcessor()

    await processor._process_task(row)
    row = await QueuedTask.objects.aget(pk=row.pk)
    assert (row.status, row.progress["deleted_through"]) == ("pending", 2)
    assert not await QueuedTask.objects.filter(task_type="log_to_channel").aexists()
    await processor._process_task(row)

    await row.arefresh_from_db()
    assert row.status == "completed"
    assert [c.args[0] for c in nextcloud.delete_team_user.call_args_list] == [1, 2, 3, 3, 4, 5]
    assert [c.args[0] for c in nextcloud.provision_team_user.call_args_list] == [1, 2, 3]
    summary = await QueuedTask.objects.aget(task_type="log_to_channel")
    assert summary.payload["message"] == "Nextcloud: deleted 5 team accounts, provisioned 3"


async def test_a_reconcile_failing_for_good_reports_its_summary_once(nextcloud):
    from core.nextcloud import NextcloudAPIError

    nextcloud.delete_team_user.side_effect = NextcloudAPIError("HTTP 502")
    payload = ReconcileNextcloud(requested_by="gold", open_event_id=7)
    row = await QueuedTask.objects.acreate(
        task_type=payload.task_type, backend="worker", payload=asdict(payload), max_retries=2
    )
    processor = WorkerQueueProcessor()
    for _ in range(2):
        await processor._process_task(await QueuedTask.objects.aget(pk=row.pk))

    await row.arefresh_from_db()
    assert row.status == "failed"
    (report,) = [t.payload["message"] async for t in QueuedTask.objects.filter(task_type="log_to_channel")]
    assert report.startswith(f"Task {row.pk} (reconcile_nextcloud) failed after 2 retries: Nextcloud: deleted 0")


async def test_reset_nextcloud_team_reprovisions_only_active(nextcloud):
    await run(ResetNextcloudTeam(team_number=4))
    await run(ResetNextcloudTeam(team_number=2))
    assert [c.args[0] for c in nextcloud.delete_team_user.call_args_list] == [4, 2]
    assert [c.args[0] for c in nextcloud.provision_team_user.call_args_list] == [2]


async def test_worker_tasks_noop_without_nextcloud(nextcloud):
    with patch("core.worker_tasks.nextcloud_configured", return_value=False):
        await run(ReconcileNextcloud(requested_by="gold", open_event_id=7))
        await run(ResetNextcloudTeam(team_number=2))
    nextcloud.__enter__.assert_not_called()


async def test_the_worker_queue_runs_only_worker_rows(nextcloud):
    worker_row = await QueuedTask.objects.acreate(
        task_type="reset_nextcloud_team", backend="worker", payload={"team_number": 2}
    )
    discord_row = await QueuedTask.objects.acreate(task_type="log_to_channel", payload={"message": "hi"})
    await WorkerQueueProcessor().process_pending()
    await worker_row.arefresh_from_db()
    await discord_row.arefresh_from_db()
    assert (worker_row.status, discord_row.status) == ("completed", "pending")


async def test_a_row_outside_the_worker_group_fails_at_once(nextcloud):
    row = await QueuedTask.objects.acreate(task_type="log_to_channel", backend="worker", payload={"message": "hi"})
    await WorkerQueueProcessor().process_pending()
    await row.arefresh_from_db()
    assert (row.status, row.retry_count, row.error_message) == ("failed", 0, "log_to_channel is not a Worker task")
