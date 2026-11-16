"""The queue processor each backend runs: poll that backend's rows, claim each one, run it and record the outcome,
retrying failures with backoff. A backend subclasses QueueProcessor with its handlers; nothing here imports a chat
client, so the worker runs it without one."""

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import ClassVar, TypedDict

from asgiref.sync import sync_to_async
from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Model, Q, QuerySet
from django.utils import timezone

from core.models import QueuedTask
from core.queued_tasks import PermanentTaskError, TaskPayload
from core.task_backends import TaskBackend
from core.utils import recycle_db_connection

logger = logging.getLogger(__name__)


class ProgressState(TypedDict, total=False):
    done: int  # steps done, for a task that counts them
    # A broadcast's: recipients reached, refused for good (with why), counts, and parts sent to each it failed on
    reached: list[int]
    refused: dict[str, str]
    sent: int
    queued: int
    parts: dict[str, int]


@dataclass
class TaskProgress:
    """What a queued task already did, saved with the task so a retry does only the rest."""

    state: ProgressState = field(default_factory=ProgressState)
    save: Callable[[ProgressState], Awaitable[None]] | None = None

    @property
    def done(self) -> int:
        return self.state.get("done", 0)

    async def advance(self) -> None:
        self.state["done"] = self.done + 1
        await self.saved()

    async def saved(self) -> None:
        if self.save:
            await self.save(self.state)


async def _target[M: Model](queryset: QuerySet[M], **lookup: object) -> M:
    """The record a task is about; its absence fails the task at once."""
    try:
        return await queryset.aget(**lookup)
    except ObjectDoesNotExist:
        raise PermanentTaskError(f"{queryset.model.__name__} {lookup} does not exist") from None


def _nothing(*_args: object) -> None:
    pass


class QueueProcessor(ABC):
    """Runs the pending rows of one backend. on_start is called with heartbeat_name in the loop's own asyncio task
    before its first poll, and on_pass after each poll and each task, so the process hosting the loop can tie them to
    its liveness heartbeat."""

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
        self._current_task: int | None = None

    @abstractmethod
    async def dispatch(self, payload: TaskPayload, progress: TaskProgress) -> Mapping[str, object] | None:
        """Run the handler for payload; what it returns is stored as the task's result. A handler given progress
        records there what it did, so a retry skips it."""

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

    async def _still_processing(self) -> bool:
        current = self._current_task
        return current is None or await QueuedTask.objects.filter(pk=current, status="processing").aexists()

    async def _process_task(self, task: QueuedTask) -> None:

        # Claim with a conditional UPDATE so a task is never handled twice.
        claimed = await QueuedTask.objects.filter(pk=task.pk, status="pending").aupdate(status="processing")
        if not claimed:
            return
        task.status = "processing"
        self._current_task = task.pk

        async def save_progress(state: ProgressState) -> None:
            await QueuedTask.objects.filter(pk=task.pk, status="processing").aupdate(progress=state)

        try:
            task_result = await self.dispatch(task.typed_payload(), TaskProgress(task.progress, save_progress))
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
        if not await QueuedTask.objects.filter(pk=task.pk, status="processing").aupdate(**outcome):
            logger.info(f"Task {task.id} ({task.task_type}) was deleted or ended while it ran")
            return False
        for name, value in outcome.items():
            setattr(task, name, value)
        return True
