"""The worker's queue: tasks no chat system takes part in, today the Nextcloud account resets."""

import asyncio
import logging
from collections.abc import Mapping

from asgiref.sync import sync_to_async
from registration.services import teardown_problem
from registration.stage import event_stage

from core.models import AuditLog, QueuedTask
from core.nextcloud import NextcloudManager, nextcloud_configured
from core.queued_tasks import LogToChannel, PermanentTaskError, ReconcileNextcloud, ResetNextcloudTeam, TaskPayload
from core.task_backends import TaskBackend
from core.task_queue import QueueProcessor, TaskProgress, _target
from core.utils import shorten
from team.models import Team

logger = logging.getLogger(__name__)


class WorkerQueueProcessor(QueueProcessor):
    backend = TaskBackend.WORKER
    heartbeat_name = "queue-worker"

    async def dispatch(self, payload: TaskPayload, progress: TaskProgress) -> Mapping[str, object] | None:
        match payload:
            case ReconcileNextcloud():
                return await self._reconcile_nextcloud(payload)
            case ResetNextcloudTeam():
                await self._reset_nextcloud_team(payload)
            case _:
                raise PermanentTaskError(f"{payload.task_type} is not a worker task")
        return None

    async def report(self, message: str) -> None:
        try:
            await sync_to_async(QueuedTask.enqueue)(LogToChannel(message=message))
        except Exception:
            logger.exception(f"Could not queue the ops report: {message}")

    async def _reconcile_nextcloud(self, payload: ReconcileNextcloud) -> dict[str, object] | None:
        if not nextcloud_configured():
            return None

        def problem() -> str:
            return teardown_problem(event_stage(), payload.open_event_id)

        if skipped := await sync_to_async(problem)():
            logger.info(f"Nextcloud reconcile skipped: {skipped}")
            return None
        teams = [t async for t in Team.objects.order_by("team_number").values_list("team_number", "is_active")]
        deleted = provisioned = 0
        stopped = ""
        failure: Exception | None = None
        try:
            with NextcloudManager() as manager:
                for number, _active in teams:
                    if stopped := await sync_to_async(problem)():
                        break
                    if await asyncio.to_thread(manager.delete_team_user, number):
                        deleted += 1
                    self.on_pass()
                if payload.open_event_id is not None and not stopped:
                    for number, active in teams:
                        if active:
                            await asyncio.to_thread(manager.provision_team_user, number)
                            provisioned += 1
                            self.on_pass()
        except Exception as e:
            failure = e
        done = {"deleted": deleted, "provisioned": provisioned, "stopped": stopped}
        await AuditLog.objects.acreate(
            action="nextcloud_reconciled",
            admin_user=payload.requested_by,
            target_entity="competition",
            target_id=payload.open_event_id or 0,
            details={"event": payload.open_event_id, **done, "finished": failure is None},
        )
        if deleted or provisioned or stopped or failure:
            await self.report(_summary(deleted, provisioned, stopped, failure))
        if failure:
            raise failure
        return done

    async def _reset_nextcloud_team(self, payload: ResetNextcloudTeam) -> None:
        if not nextcloud_configured():
            return
        team = await _target(Team.objects.all(), team_number=payload.team_number)

        def reset() -> None:
            with NextcloudManager() as manager:
                manager.delete_team_user(team.team_number)
                if team.is_active:
                    manager.provision_team_user(team.team_number)

        await asyncio.to_thread(reset)


def _summary(deleted: int, provisioned: int, stopped: str, failure: Exception | None) -> str:
    line = f"Nextcloud: deleted {deleted} team accounts, provisioned {provisioned}"
    if stopped:
        line += f"; stopped before the end: {stopped}"
    if failure is not None:
        line += f"; failed partway: {shorten(str(failure), 200)}"
    return line
