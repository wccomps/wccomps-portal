"""The worker's queue: tasks no chat system takes part in, today the Nextcloud account resets."""

import asyncio
import logging
from collections.abc import Callable, Mapping
from typing import TypedDict, assert_never

from asgiref.sync import sync_to_async
from django.db import transaction
from registration.services import teardown_problem
from registration.stage import event_stage

from core.heartbeat import QUEUE_WORKER
from core.models import AuditLog, QueuedTask
from core.nextcloud import NextcloudManager, nextcloud_configured
from core.queued_tasks import ReconcileNextcloud, ResetNextcloudTeam, WorkerTask
from core.services.ops import notify_ops
from core.task_backends import TaskBackend
from core.task_queue import QueueProcessor, TaskProgress, _target
from core.utils import shorten
from team.models import Team

logger = logging.getLogger(__name__)


class ReconcileState(TypedDict, total=False):
    """A Nextcloud reconcile's progress: accounts deleted and provisioned, and the last team number each pass
    finished."""

    deleted: int
    provisioned: int
    deleted_through: int
    provisioned_through: int


class WorkerQueueProcessor(QueueProcessor[WorkerTask]):
    backend = TaskBackend.WORKER
    heartbeat_name = QUEUE_WORKER

    async def dispatch(self, payload: WorkerTask, task: QueuedTask) -> Mapping[str, object] | None:
        match payload:
            case ReconcileNextcloud():
                return await self._reconcile_nextcloud(payload, TaskProgress.of(task))
            case ResetNextcloudTeam():
                await self._reset_nextcloud_team(payload)
            case _:
                assert_never(payload)
        return None

    async def report(self, message: str) -> None:
        try:
            await sync_to_async(notify_ops)(message)
        except Exception:
            logger.exception(f"Could not queue the ops report: {message}")

    async def _reconcile_nextcloud(
        self, payload: ReconcileNextcloud, progress: TaskProgress[ReconcileState]
    ) -> dict[str, object] | None:
        """Each team's account is recorded in progress once done, so a retry resumes after the last one; the summary is
        posted when the task ends: here, or in the error the last failed attempt reports. A retry refused up front
        ends there, stopped, reporting what the earlier attempts did."""
        if not nextcloud_configured():
            return None

        def problem() -> str:
            return teardown_problem(event_stage(), payload.open_event_id)

        state = progress.state
        stopped = await sync_to_async(problem)()
        if stopped and not state:
            logger.info(f"Nextcloud reconcile skipped: {stopped}")
            return None
        failure: Exception | None = None
        try:
            if not stopped:
                stopped = await self._reconcile_teams(payload, progress, problem)
        except Exception as e:
            failure = e
        deleted, provisioned = state.get("deleted", 0), state.get("provisioned", 0)
        done = {"deleted": deleted, "provisioned": provisioned, "stopped": stopped}

        def record() -> None:
            # One transaction: the audit row and the queued summary are written together or not at all
            with transaction.atomic():
                AuditLog.objects.create(
                    action="nextcloud_reconciled",
                    admin_user=payload.requested_by,
                    target_entity="competition",
                    target_id=payload.open_event_id or 0,
                    details={"event": payload.open_event_id, **done, "finished": failure is None},
                )
                if failure is None and (deleted or provisioned or stopped):
                    notify_ops(_summary(deleted, provisioned, stopped, None), actor=payload.requested_by)

        await sync_to_async(record)()
        if failure:
            raise RuntimeError(_summary(deleted, provisioned, stopped, failure)) from failure
        return done

    async def _reconcile_teams(
        self, payload: ReconcileNextcloud, progress: TaskProgress[ReconcileState], problem: Callable[[], str]
    ) -> str:
        """Delete each team's account, then provision the active teams' when an event is open; what made it stop
        before the end, if anything."""
        state = progress.state
        teams = [t async for t in Team.objects.order_by("team_number").values_list("team_number", "is_active")]
        with NextcloudManager() as manager:
            for number, _active in teams:
                if number <= state.get("deleted_through", -1):
                    continue
                if stopped := await sync_to_async(problem)():
                    return stopped
                if await asyncio.to_thread(manager.delete_team_user, number):
                    state["deleted"] = state.get("deleted", 0) + 1
                state["deleted_through"] = number
                await progress.saved()
                self.on_pass()
            if payload.open_event_id is not None:
                for number, active in teams:
                    if active and number > state.get("provisioned_through", -1):
                        await asyncio.to_thread(manager.provision_team_user, number)
                        state["provisioned"] = state.get("provisioned", 0) + 1
                        state["provisioned_through"] = number
                        await progress.saved()
                        self.on_pass()
        return ""

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
