"""The preflight report: the competition timer queues it for ops 30 and 10 minutes before the scheduled start, once
each per start time, even across a worker restart."""

from datetime import UTC, datetime, timedelta

import pytest
from asgiref.sync import sync_to_async
from quotient.client import EngineStatus, RoundCheck
from scoring import preflight

from core.models import AuditLog, CompetitionConfig, QueuedTask
from core.queued_tasks import LogToChannel
from core.services import competition
from worker import jobs

START = datetime(2026, 11, 21, 17, 0, tzinfo=UTC)


class _Quotient:
    def __init__(self) -> None:
        self.status: EngineStatus | None = None
        self.reads = 0

    def get_engine_status(self) -> EngineStatus | None:
        self.reads += 1
        return self.status


@pytest.fixture
def clock(monkeypatch):
    """Sets the time the timer pass sees."""
    now = {"at": START - timedelta(hours=1)}
    monkeypatch.setattr(jobs.timezone, "now", lambda: now["at"])
    return now


@pytest.fixture
def quotient(monkeypatch, settings):
    settings.QUOTIENT_API_URL = "https://quotient.test"
    fake = _Quotient()
    monkeypatch.setattr(preflight, "get_quotient_client", lambda: fake)
    return fake


async def _schedule(start: datetime | None = START, enabled: bool = False) -> None:
    await CompetitionConfig.objects.aupdate_or_create(
        pk=1, defaults={"competition_start_time": start, "applications_enabled": enabled}
    )


async def _posted() -> list[str]:
    """The ops notices queued, in order, each beside its audit row."""
    payloads = [t.typed_payload() async for t in QueuedTask.objects.order_by("pk")]
    posts = [p.message for p in payloads if isinstance(p, LogToChannel)]
    audited = [a.details["message"] async for a in AuditLog.objects.filter(action="ops_notice").order_by("pk")]
    assert audited == posts
    return posts


async def _pass_at(clock: dict[str, datetime], at: datetime) -> None:
    clock["at"] = at
    await jobs.check_competition_times()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
class TestPreflightReport:
    async def test_queued_at_30_and_at_10_minutes_before_the_start_once_each(self, clock, quotient):
        await _schedule()
        for minutes in (31, 30, 29, 11, 10, 9, 1):
            quotient.status = EngineStatus(
                running=True,
                preflight=True,
                round_id=4,
                round_start=START - timedelta(minutes=minutes),
                checks=[RoundCheck(team_id=2, service_name="web-http", passed=False)],
            )
            await _pass_at(clock, START - timedelta(minutes=minutes))

        posts = await _posted()
        assert quotient.reads == 2
        assert len(posts) == 2
        assert all("- **Team 2**: web-http" in p for p in posts)

    async def test_a_queued_report_is_recorded_so_no_later_pass_or_restart_queues_it_again(self, clock, quotient):
        await _schedule()
        await _pass_at(clock, START - timedelta(minutes=5))

        await _pass_at(clock, START - timedelta(minutes=4))

        assert quotient.reads == 1
        assert len(await _posted()) == 1
        recorded = AuditLog.objects.filter(action=competition.PreflightMark.ACTION)
        assert [a.details async for a in recorded] == [{"start_time": START.isoformat(), "minutes": 10}]

    async def test_a_new_start_time_gets_its_own_reports(self, clock, quotient):
        await _schedule()
        await _pass_at(clock, START - timedelta(minutes=10))

        await _schedule(START + timedelta(hours=1))
        await _pass_at(clock, START + timedelta(minutes=50))

        assert len(await _posted()) == 2

    @pytest.mark.parametrize(
        ("start", "enabled", "minutes_before"),
        [(None, False, 10), (START, True, 10), (START, False, 31)],
        ids=["no start time", "competition running", "too early"],
    )
    async def test_nothing_is_queued_outside_the_marks_before_a_scheduled_start(
        self, clock, quotient, start, enabled, minutes_before
    ):
        await _schedule(start, enabled)

        await _pass_at(clock, START - timedelta(minutes=minutes_before))

        assert quotient.reads == 0
        assert await _posted() == []

    async def test_nothing_is_queued_while_quotient_is_not_configured(self, clock, quotient, settings):
        settings.QUOTIENT_API_URL = ""
        await _schedule()

        await _pass_at(clock, START - timedelta(minutes=10))

        assert quotient.reads == 0
        assert await _posted() == []

    async def test_an_unreachable_quotient_is_reported(self, clock, quotient):
        await _schedule()

        await _pass_at(clock, START - timedelta(minutes=10))

        assert await _posted() == ["**Preflight:** couldn't read the engine status from Quotient."]

    async def test_a_schedule_moved_while_quotient_was_read_queues_nothing(self, clock, quotient):
        await _schedule()
        clock["at"] = START - timedelta(minutes=10)
        mark = await sync_to_async(competition.due_preflight_report)(clock["at"])
        assert mark is not None

        await _schedule(START + timedelta(hours=2))

        await sync_to_async(competition.queue_preflight_report)(mark, "report", clock["at"])
        assert await _posted() == []

    async def test_a_failed_queue_leaves_the_report_due(self, clock, quotient, monkeypatch):
        """The record and the queued notice commit together, so a failure retries on the next pass."""
        await _schedule()

        def broken(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("database went away")

        monkeypatch.setattr(competition, "notify_ops", broken)
        await _pass_at(clock, START - timedelta(minutes=10))
        monkeypatch.undo()
        monkeypatch.setattr(jobs.timezone, "now", lambda: clock["at"])
        monkeypatch.setattr(preflight, "get_quotient_client", lambda: quotient)

        await _pass_at(clock, START - timedelta(minutes=9))

        assert len(await _posted()) == 1
