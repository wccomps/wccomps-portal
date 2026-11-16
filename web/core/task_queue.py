"""The queue processor each backend runs: poll that backend's rows, claim each one, run it and record the outcome,
retrying failures with backoff. A backend subclasses QueueProcessor with its handlers; nothing here imports a chat
client, so the worker runs it without one."""

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import ClassVar, TypedDict, cast

from asgiref.sync import sync_to_async
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.db.models import Model, Q, QuerySet
from django.utils import timezone

from core.models import QueuedTask
from core.queued_tasks import BACKEND_TASKS, PermanentTaskError, TaskPayload
from core.task_backends import TaskBackend
from core.utils import recycle_db_connection

logger = logging.getLogger(__name__)


class StepsState(TypedDict, total=False):
    """The state of a task that counts its steps: a message's parts sent, or a recreate's delete."""

    done: int


def _running(task_id: int) -> QuerySet[QueuedTask]:
    """The task's row while it still runs: empty once it was deleted, or a reset failed or scrubbed it."""
    return QueuedTask.objects.filter(pk=task_id, status="processing")


@dataclass
class TaskProgress[S: Mapping[str, object]]:
    """What a queued task already did, its handler's state S, saved with the task so a retry does only the rest."""

    task_id: int
    save: Callable[[S], Awaitable[None]]
    state: S

    @classmethod
    def of(cls, task: QueuedTask) -> TaskProgress[S]:
        """The progress of a claimed task, saved to its row while the task still runs."""

        async def save(state: S) -> None:
            await _running(task.pk).aupdate(progress=state)

        return cls(task.pk, save, task.progress)

    @classmethod
    def in_memory(cls, state: S) -> TaskProgress[S]:
        """Progress of work no queued task runs: saving keeps it in state only. Its task_id, 0, names no row, so it is
        only for counting steps (send_parts), not for still_running or while_running."""

        async def save(_state: S) -> None:
            pass

        return cls(0, save, state)

    @property
    def done(self: TaskProgress[StepsState]) -> int:
        return self.state.get("done", 0)

    async def advance(self: TaskProgress[StepsState]) -> None:
        self.state["done"] = self.done + 1
        await self.saved()

    async def saved(self) -> None:
        await self.save(self.state)

    async def still_running(self) -> bool:
        """Whether the task still runs: False once it was deleted, or a reset failed or scrubbed it."""
        return await _running(self.task_id).aexists()

    async def while_running(self, work: Callable[[], object]) -> bool:
        """Do work (sync ORM writes) in one transaction holding the task's row lock, only while the task still runs:
        False, with nothing done, once it doesn't. A reset failing the task waits on the lock meanwhile."""
        task_id = self.task_id

        @sync_to_async
        def run() -> bool:
            with transaction.atomic():
                if not _running(task_id).select_for_update(no_key=True).exists():
                    return False
                work()
                return True

        return await run()


async def _target[M: Model](queryset: QuerySet[M], **lookup: object) -> M:
    """The record a task is about; its absence fails the task at once."""
    try:
        return await queryset.aget(**lookup)
    except ObjectDoesNotExist:
        raise PermanentTaskError(f"{queryset.model.__name__} {lookup} does not exist") from None


def _nothing(*_args: object) -> None:
    pass


class QueueProcessor[P: TaskPayload](ABC):
    """Runs the pending rows of one backend. on_start is called with heartbeat_name in the loop's own asyncio task
    before its first poll, and on_pass after each poll and each task, so the process hosting the loop can tie them to
    its liveness heartbeat. P is the payload groups core.queued_tasks.BACKEND_TASKS gives the backend: a row of any
    other type fails at once."""

    backend: ClassVar[TaskBackend]
    heartbeat_name: ClassVar[str]

    QUEUE_POLL_INTERVAL_SECONDS = 2
    QUEUE_BATCH_SIZE = 10
    MAX_BACKOFF_SECONDS = 300
    # A task left in "processing" by a dead process is retried if younger than this, failed if older.
    STRANDED_TASK_MAX_AGE = timedelta(hours=1)

    def __init__(self, *, on_start: Callable[[str], None] = _nothing, on_pass: Callable[[], None] = _nothing) -> None:
        self.on_start = on_start
        self.on_pass = on_pass
        self.running = False
        self.task: asyncio.Task[None] | None = None

    @abstractmethod
    async def dispatch(self, payload: P, task: QueuedTask) -> Mapping[str, object] | None:
        """Run the handler for payload; what it returns is stored as the task's result. A handler that resumes takes
        TaskProgress.of(task) and records there what it did, so a retry skips it."""

    @abstractmethod
    async def report(self, message: str) -> None:
        """Tell ops something, such as a task that failed for good. Never raises."""

    def retry_after(self, error: Exception) -> float | None:
        """The least wait before retrying after error, when the backend says one (a rate limit)."""
        return None

    def start(self) -> None:
        self.running = True
        self.task = asyncio.create_task(self._process_loop())
        logger.info(f"{self.backend.label} queue processor started")

    def stop(self) -> None:
        self.running = False
        if self.task:
            self.task.cancel()
        logger.info(f"{self.backend.label} queue processor stopped")

    async def _process_loop(self) -> None:
        self.on_start(self.heartbeat_name)
        try:
            await self.recover_stranded()
        except Exception:
            logger.exception(f"Failed to recover stranded {self.backend} queue tasks")

        while self.running:
            try:
                await recycle_db_connection()
                await self.process_pending()
            except Exception as e:
                logger.exception(f"Error in {self.backend} queue processor: {e}")

            await asyncio.sleep(self.QUEUE_POLL_INTERVAL_SECONDS)

    async def recover_stranded(self) -> None:
        """Requeue this backend's tasks a previous process left in "processing" when it died.

        Runs before the first poll. Each backend's queue runs in one process (one replica, Recreate strategy), so
        no other processor can be working on these rows.
        """
        backend, cutoff = self.backend, timezone.now() - self.STRANDED_TASK_MAX_AGE

        @sync_to_async
        def recover() -> tuple[int, int]:
            stranded = QueuedTask.objects.filter(backend=backend, status="processing")
            requeued = stranded.filter(created_at__gte=cutoff).update(status="pending", next_retry_at=None)
            failed = stranded.filter(created_at__lt=cutoff).update(
                status="failed", error_message="Left in processing by a restart; too old to retry"
            )
            return requeued, failed

        requeued, failed = await recover()
        if requeued or failed:
            logger.warning(f"Recovered stranded {backend} queue tasks: {requeued} requeued, {failed} failed as too old")

    async def process_pending(self) -> None:
        """Run up to QUEUE_BATCH_SIZE of this backend's due tasks, oldest first."""
        backend, batch = self.backend, self.QUEUE_BATCH_SIZE

        @sync_to_async
        def get_pending_tasks() -> list[QueuedTask]:
            now = timezone.now()
            return list(
                QueuedTask.objects.filter(backend=backend, status="pending")
                .filter(Q(next_retry_at__isnull=True) | Q(next_retry_at__lte=now))
                .order_by("created_at")[:batch]
            )

        tasks = await get_pending_tasks()
        self.on_pass()

        for task in tasks:
            await self._process_task(task)
            self.on_pass()

    def _payload(self, task: QueuedTask) -> P:
        payload = task.typed_payload()
        if not isinstance(payload, BACKEND_TASKS[self.backend]):
            raise PermanentTaskError(f"{task.task_type} is not a {self.backend.label} task")
        # P is the backend's groups in BACKEND_TASKS, which mypy can't tie to the map's entry
        return cast(P, payload)

    async def _process_task(self, task: QueuedTask) -> None:

        # Claim with a conditional UPDATE so a task is never handled twice.
        claimed = await QueuedTask.objects.filter(pk=task.pk, status="pending").aupdate(status="processing")
        if not claimed:
            return
        task.status = "processing"

        try:
            task_result = await self.dispatch(self._payload(task), task)
        except PermanentTaskError as error:
            if await self._record(task, status="failed", error_message=str(error)):
                logger.error(f"Task {task.id} ({task.task_type}) can never succeed: {error}")
                await self.report(f"Task {task.id} ({task.task_type}) failed and was not retried: {error}")
        except Exception as error:
            retry_count = task.retry_count + 1
            if retry_count >= task.max_retries:
                if await self._record(task, status="failed", retry_count=retry_count, error_message=str(error)):
                    logger.exception(f"Task {task.id} failed after {task.max_retries} retries: {error}")
                    await self.report(
                        f"Task {task.id} ({task.task_type}) failed after {task.max_retries} retries: {error}"
                    )
                return
            backoff_seconds = max(min(2**retry_count, self.MAX_BACKOFF_SECONDS), self.retry_after(error) or 0)
            if await self._record(
                task,
                status="pending",
                retry_count=retry_count,
                error_message=str(error),
                next_retry_at=timezone.now() + timedelta(seconds=backoff_seconds),
            ):
                logger.warning(f"Task {task.id} failed (attempt {retry_count}), retrying in {backoff_seconds}s")
        else:
            result = dict(task_result) if task_result is not None else None
            if await self._record(task, status="completed", completed_at=timezone.now(), result=result):
                logger.info(f"Completed task {task.id}: {task.task_type}")

    @staticmethod
    async def _record(task: QueuedTask, **outcome: object) -> bool:
        """Write the task's outcome only while the row is still this processor's to finish: False when deleted or when
        a reset failed or scrubbed it meanwhile."""
        if not await _running(task.pk).aupdate(**outcome):
            logger.info(f"Task {task.id} ({task.task_type}) was deleted or ended while it ran")
            return False
        for name, value in outcome.items():
            setattr(task, name, value)
        return True
