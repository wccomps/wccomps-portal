"""The worker's preflight report: queued for the ops channel 30 and 10 minutes before the scheduled start."""

from datetime import UTC, datetime, timedelta

import pytest
from quotient.client import EngineStatus, RoundCheck

from core.models import AuditLog, CompetitionConfig, QueuedTask
from core.queued_tasks import LogToChannel
from worker import jobs
from worker.tests.loops import one_pass

START = datetime(2026, 11, 21, 17, 0, tzinfo=UTC)


class _Quotient:
    def __init__(self, status: EngineStatus | None, names: dict[int, str] | None = None) -> None:
        self.status = status
        self.names = names or {}
        self.reads = 0

    def get_engine_status(self) -> EngineStatus | None:
        self.reads += 1
        return self.status

    def get_team_names(self) -> dict[int, str] | None:
        return self.names


def _status(round_start: datetime, checks: list[RoundCheck]) -> EngineStatus:
    return EngineStatus(running=True, preflight=True, round_id=4, round_start=round_start, checks=checks)


@pytest.fixture(autouse=True)
def hb_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_HEARTBEAT_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def clock(monkeypatch):
    """Sets the time the worker's pass sees."""
    now = {"at": START - timedelta(hours=1)}
    monkeypatch.setattr(jobs.timezone, "now", lambda: now["at"])
    return now


@pytest.fixture
def quotient(monkeypatch):
    fake = _Quotient(None)
    monkeypatch.setattr(jobs, "get_quotient_client", lambda: fake)
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


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
class TestPreflightReports:
    async def test_posts_at_30_and_at_10_minutes_before_the_start_once_each(self, clock, quotient):
        await _schedule()
        reports = jobs.PreflightReports()
        for minutes in (31, 30, 29, 11, 10, 9, 1):
            clock["at"] = START - timedelta(minutes=minutes)
            quotient.status = _status(clock["at"], [RoundCheck(team_id=1, service_name="web-http", passed=True)])
            await reports.run_pass()

        assert quotient.reads == 2
        assert len(await _posted()) == 2

    async def test_a_worker_started_inside_the_last_window_posts_once(self, clock, quotient):
        await _schedule()
        clock["at"] = START - timedelta(minutes=5)
        reports = jobs.PreflightReports()

        await reports.run_pass()
        await reports.run_pass()

        assert len(await _posted()) == 1

    async def test_a_new_start_time_gets_its_own_reports(self, clock, quotient):
        await _schedule()
        reports = jobs.PreflightReports()
        clock["at"] = START - timedelta(minutes=10)
        await reports.run_pass()

        await _schedule(START + timedelta(hours=1))
        clock["at"] = START + timedelta(minutes=50)
        await reports.run_pass()

        assert len(await _posted()) == 2

    @pytest.mark.parametrize(
        ("start", "enabled", "minutes_before"),
        [(None, False, 10), (START, True, 10), (START, False, 0), (START, False, -5), (START, False, 31)],
        ids=["no start time", "competition running", "at the start", "after the start", "too early"],
    )
    async def test_posts_nothing_outside_the_windows_before_a_scheduled_start(
        self, clock, quotient, start, enabled, minutes_before
    ):
        await _schedule(start, enabled)
        clock["at"] = START - timedelta(minutes=minutes_before)

        await jobs.PreflightReports().run_pass()

        assert quotient.reads == 0
        assert await _posted() == []

    async def test_an_unreachable_quotient_is_reported(self, clock, quotient):
        await _schedule()
        clock["at"] = START - timedelta(minutes=10)

        await jobs.PreflightReports().run_pass()

        assert await _posted() == ["**Preflight:** couldn't read the engine status from Quotient."]

    async def test_a_long_report_is_queued_in_parts_that_fit_and_lose_no_team(self, clock, quotient):
        await _schedule()
        clock["at"] = START - timedelta(minutes=10)
        services = [f"service-{n:02}-with-a-long-name" for n in range(12)]
        quotient.status = _status(
            clock["at"],
            [RoundCheck(team_id=t, service_name=s, passed=False) for t in range(1, 51) for s in services],
        )
        quotient.names = {t: f"team{t:02}" for t in range(1, 51)}

        await jobs.PreflightReports().run_pass()

        posts = await _posted()
        assert len(posts) > 1
        assert all(len(p) <= jobs.OPS_MESSAGE_CHARS for p in posts)
        lines = "\n".join(posts).splitlines()
        assert [line.split("**")[1] for line in lines[1:]] == [f"team{t:02}" for t in range(1, 51)]

    async def test_the_loop_beats_with_quotient_unreachable(self, clock, quotient, hb_dir, monkeypatch):
        await _schedule()
        clock["at"] = START - timedelta(minutes=10)

        await one_pass(jobs.preflight_report_loop, monkeypatch)

        assert (hb_dir / "preflight").exists()
        assert len(await _posted()) == 1
