"""Tests for competition timer background task."""

import asyncio
import contextlib
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from django.utils import timezone

from bot.competition_timer import CompetitionTimer
from core.models import AuditLog, CompetitionConfig
from core.services.competition import Attempt, CompetitionRunResult


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
class TestCompetitionTimer:
    """Test competition timer functionality."""

    async def test_check_competition_times_no_enable_needed(self) -> None:
        """Test _check_competition_times when applications should not be enabled."""
        bot = AsyncMock(spec=discord.Client)
        timer = CompetitionTimer(bot)

        # Use update_or_create with pk=1 to match get_config() singleton pattern
        config, _ = await CompetitionConfig.objects.aupdate_or_create(
            pk=1,
            defaults={
                "competition_start_time": timezone.now() + timedelta(hours=1),
                "applications_enabled": False,
                "controlled_applications": ["app1", "app2"],
            },
        )

        await timer._check_competition_times()

        await config.arefresh_from_db()
        assert config.applications_enabled is False

    async def test_check_competition_times_calls_start_competition(self) -> None:
        """Test _check_competition_times calls start_competition when scheduled."""
        bot = AsyncMock(spec=discord.Client)
        timer = CompetitionTimer(bot)

        # Use update_or_create with pk=1 to match get_config() singleton pattern
        await CompetitionConfig.objects.aupdate_or_create(
            pk=1,
            defaults={
                "competition_start_time": timezone.now() - timedelta(minutes=5),
                "applications_enabled": False,
                "controlled_applications": ["app1"],
            },
        )

        with patch("bot.competition_timer.run_competition", new_callable=AsyncMock) as mock_start:
            mock_start.return_value = CompetitionRunResult(enable=True, controlled_apps=["app1"], apps_ok=["app1"])
            with (
                patch("bot.competition_timer.log_to_ops_channel", new_callable=AsyncMock),
                patch("bot.competition_timer.update_status_channel", new_callable=AsyncMock),
            ):
                await timer._check_competition_times()

            mock_start.assert_called_once()
            assert mock_start.call_args.args == (True,)
            assert mock_start.call_args.kwargs["actor"] == "timer"

    async def test_check_competition_times_exception_handling(self) -> None:
        """Test _check_competition_times handles exceptions."""
        bot = AsyncMock(spec=discord.Client)
        timer = CompetitionTimer(bot)

        # Use update_or_create with pk=1 to match get_config() singleton pattern
        await CompetitionConfig.objects.aupdate_or_create(
            pk=1,
            defaults={
                "competition_start_time": timezone.now() - timedelta(minutes=5),
                "applications_enabled": False,
                "controlled_applications": ["app1"],
            },
        )

        with patch("bot.competition_timer.run_competition", new_callable=AsyncMock) as mock_start:
            mock_start.side_effect = Exception("Start error")

            with patch("bot.competition_timer.log_to_ops_channel", new_callable=AsyncMock):
                # Should not raise exception
                await timer._check_competition_times()

    async def test_check_competition_times_with_no_config(self) -> None:
        """Test _check_competition_times handles missing config."""
        bot = AsyncMock(spec=discord.Client)
        timer = CompetitionTimer(bot)

        # Should not raise exception - get_config creates if missing
        await timer._check_competition_times()


@pytest.mark.asyncio
@pytest.mark.parametrize("work_fails", [False, True])
async def test_each_pass_recycles_then_beats_only_on_success(work_fails, monkeypatch) -> None:
    """A pass never raises (tasks.loop would stop), and a failed one skips the liveness heartbeat."""
    from bot import competition_timer, heartbeat

    timer = CompetitionTimer(AsyncMock(spec=discord.Client))
    calls: list[str] = []

    async def work() -> None:
        calls.append("work")
        if work_fails:
            raise RuntimeError("db down")

    async def recycle() -> None:
        calls.append("recycle")

    monkeypatch.setattr(competition_timer, "recycle_db_connection", recycle)
    monkeypatch.setattr(heartbeat, "record", lambda name, bot: calls.append(f"beat:{name}"))
    monkeypatch.setattr(timer, "_check_competition_times", work)

    await timer.check_loop()

    assert calls == ["recycle", "work"] if work_fails else ["recycle", "work", "beat:timer"]


@pytest.mark.asyncio
async def test_config_read_failing_on_the_db_skips_the_beat(monkeypatch) -> None:
    """The real work must let a DB error through, or the liveness probe never sees it."""
    from bot import competition_timer, heartbeat

    timer = CompetitionTimer(AsyncMock(spec=discord.Client))
    beats: list[str] = []
    monkeypatch.setattr(competition_timer, "recycle_db_connection", AsyncMock())
    monkeypatch.setattr(heartbeat, "record", lambda name, bot: beats.append(name))

    with patch("core.models.CompetitionConfig.get_config", side_effect=RuntimeError("db down")):
        await timer.check_loop()

    assert beats == []


@pytest.mark.asyncio
async def test_loop_waits_for_ready_and_stops_with_the_bot() -> None:
    """Loaded like production: nothing runs before the gateway is ready, and closing the bot cancels the loop."""
    from discord.ext import commands

    async with commands.Bot(command_prefix="!", intents=discord.Intents.default()) as bot:
        timer = CompetitionTimer(bot)
        with patch.object(timer, "_check_competition_times", new_callable=AsyncMock) as work:
            await bot.add_cog(timer)
            assert timer.check_loop.is_running()
            await asyncio.sleep(0.1)
            work.assert_not_awaited()
    assert not timer.check_loop.is_running()


def _run(failed_accounts: int | None) -> CompetitionRunResult:
    if failed_accounts is None:
        return CompetitionRunResult(enable=True, error="No controlled applications configured")
    result = CompetitionRunResult(enable=True, controlled_apps=["scoring"], apps_ok=["scoring"], accounts_total=3)
    result.accounts_ok, result.accounts_failed = 3 - failed_accounts, failed_accounts
    return result


async def _start_due() -> None:
    await CompetitionConfig.objects.aupdate_or_create(
        pk=1,
        defaults={"competition_start_time": timezone.now() - timedelta(minutes=1), "applications_enabled": False},
    )


async def _series(
    enable: bool | None, attempt: int = 0, started: datetime | None = None, schedule: datetime | None = None
) -> None:
    await CompetitionConfig.objects.filter(pk=1).aupdate(
        timer_series_enable=enable,
        timer_series_attempt=attempt,
        timer_series_started=started,
        timer_series_schedule=schedule,
    )


def _fake_runs(outcomes: list[int | None | str]):
    """Stand-in for run_competition that leaves the config as the real one does: a count of accounts left unchanged,
    None for a run that errors, "raise" for one that raises."""
    calls = iter(outcomes)

    async def run(enable: bool, actor: str, timer: Attempt | None = None) -> CompetitionRunResult:
        outcome = next(calls)
        if outcome is None or outcome == "raise":
            await _series(enable, timer.number, timer.first_started, timer.schedule)
            if outcome == "raise":
                raise RuntimeError("Authentik timed out")
            return _run(None)
        assert not isinstance(outcome, str)
        await CompetitionConfig.objects.filter(pk=1).aupdate(applications_enabled=enable, competition_start_time=None)
        if outcome:
            await _series(enable, timer.number, timer.first_started, timer.schedule)
        else:
            await _series(None)
        return _run(outcome)

    return run


async def _ticks(count: int, runs: list[int | None | str], *, restart_after: int | None = None, ops_raise=False):
    """Tick a timer count times (a new timer after restart_after ticks); the run mock and the ops posts."""
    timer = CompetitionTimer(AsyncMock(spec=discord.Client))
    ops_side_effect = RuntimeError("Discord down") if ops_raise else None
    with (
        patch("bot.competition_timer.run_competition", new=AsyncMock(side_effect=_fake_runs(runs))) as start,
        patch("bot.competition_timer.log_to_ops_channel", new=AsyncMock(side_effect=ops_side_effect)) as ops,
        patch("bot.competition_timer.update_status_channel", new_callable=AsyncMock) as status,
    ):
        for tick in range(count):
            if tick == restart_after:
                timer = CompetitionTimer(AsyncMock(spec=discord.Client))
            with contextlib.suppress(RuntimeError):
                await timer._check_competition_times()
    return start, [call.args[1] for call in ops.await_args_list], status


GAVE_UP = (
    "**Competition Auto-Start Incomplete:** the timer stopped retrying, as still incomplete after 4 attempts. "
    "Run Start again from the competition page."
)


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_an_incomplete_first_attempt_is_announced_and_a_later_one_completes() -> None:
    await _start_due()

    start, posts, status = await _ticks(4, [1, 0])

    assert start.await_count == 2
    assert [p.split("**")[1] for p in posts] == ["Competition Auto-Started, Incomplete:", "Competition Auto-Started!"]
    assert posts[1].endswith("Completed on attempt 2.")
    assert status.await_count == 2


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_giving_up_is_reported_once_with_a_status_update() -> None:
    await _start_due()

    start, posts, status = await _ticks(8, [2, 2, 1, 1], restart_after=6)

    assert start.await_count == 4
    assert posts[1:] == [GAVE_UP]
    assert status.await_count == 2
    config = await CompetitionConfig.objects.aget(pk=1)
    assert (config.timer_series_enable, config.timer_series_attempt) == (None, 0)
    assert await AuditLog.objects.filter(action="competition_timer_gave_up").acount() == 1


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("failure", [None, "raise"], ids=["errors", "raises"])
async def test_a_retry_that_errors_counts_as_an_attempt(failure) -> None:
    await _start_due()

    start, posts, _status = await _ticks(9, [2, failure, failure, failure, 0], restart_after=3)

    assert start.await_count == 4
    assert posts[1:] == [GAVE_UP]


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("failure", [None, "raise"], ids=["errors", "raises"])
async def test_a_first_attempt_that_keeps_failing_is_reported_once_and_given_up_after_four(failure) -> None:
    await _start_due()

    start, posts, _status = await _ticks(9, [failure] * 4 + [0], restart_after=5)

    assert start.await_count == 4
    first = (
        "**Competition Auto-Start Failed:** No controlled applications configured"
        if failure is None
        else "**Error in competition timer:** Authentik timed out"
    )
    assert posts == [first, GAVE_UP]
    assert (await CompetitionConfig.objects.aget(pk=1)).competition_start_time is None


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_a_slow_series_past_a_quarter_hour_still_retries_and_reports() -> None:
    """A stop of 50 slow team accounts can take a quarter of an hour by itself."""
    await CompetitionConfig.objects.aupdate_or_create(pk=1, defaults={"applications_enabled": True})
    await _series(False, 1, timezone.now() - timedelta(minutes=20))

    start, posts, _status = await _ticks(2, [0])

    assert start.await_count == 1
    assert posts[-1].endswith("Completed on attempt 2.")


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_a_restart_past_the_window_reports_giving_up_once_and_runs_nothing() -> None:
    await CompetitionConfig.objects.aupdate_or_create(pk=1, defaults={"applications_enabled": True})
    await _series(True, 1, timezone.now() - timedelta(hours=2))

    start, posts, _status = await _ticks(3, [0])

    start.assert_not_awaited()
    assert len(posts) == 1
    assert "its first attempt was over 60 minutes ago" in posts[0]


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_giving_up_on_a_series_ended_meanwhile_reports_nothing() -> None:
    """A run by hand or a new time ended the series between the timer's plan and its give-up; nothing stopped."""
    from core.services.competition import end_timer_series, give_up_timer_series

    await CompetitionConfig.objects.aupdate_or_create(pk=1, defaults={"applications_enabled": False})
    await _series(True, 4, timezone.now() - timedelta(minutes=10))

    def ended_first(plan):
        end_timer_series(True)
        return give_up_timer_series(plan)

    with patch("bot.competition_timer.give_up_timer_series", ended_first):
        start, posts, status = await _ticks(1, [])

    start.assert_not_awaited()
    assert posts == []
    status.assert_not_awaited()
    assert not await AuditLog.objects.filter(action="competition_timer_gave_up").aexists()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_a_run_by_hand_after_an_incomplete_auto_start_ends_the_retries() -> None:
    """A run by hand that finishes clears the timer's series, whatever it changed."""
    await _start_due()
    await _ticks(1, [2])
    await _series(None)

    start, posts, _status = await _ticks(3, [2])

    start.assert_not_awaited()
    assert posts == []


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_a_complete_auto_start_runs_once_even_when_reporting_it_fails() -> None:
    await _start_due()

    start, _posts, _status = await _ticks(3, [0], ops_raise=True)

    start.assert_awaited_once()
    config = await CompetitionConfig.objects.aget(pk=1)
    assert (config.timer_series_enable, config.timer_series_attempt) == (None, 0)


@pytest.mark.asyncio
async def test_a_run_of_many_quick_steps_keeps_the_timer_alive(monkeypatch) -> None:
    """A start can take longer than the timer's budget when Authentik is slow; each finished step beats."""
    from bot import competition_actions, competition_timer, heartbeat

    beats: list[str] = []
    monkeypatch.setattr(heartbeat, "record", lambda name, bot: beats.append(name))
    monkeypatch.setattr(competition_timer, "recycle_db_connection", AsyncMock())
    during: list[int] = []

    def steps(enable: bool, actor: str, timer: object = None):
        for n in range(5):
            during.append(len(beats))
            yield n
        return CompetitionRunResult(enable=enable)

    monkeypatch.setattr(competition_actions, "competition_steps", steps)
    timer = CompetitionTimer(AsyncMock(spec=discord.Client))

    async def check() -> None:
        await competition_actions.run_competition(True, "timer")

    monkeypatch.setattr(timer, "_check_competition_times", check)

    await timer.check_loop()

    assert during == [0, 1, 2, 3, 4]
    assert beats == ["timer"] * 6


@pytest.mark.asyncio
async def test_a_wedged_step_stops_the_timer_heartbeat(monkeypatch) -> None:
    """A step stuck on a half-open socket must let the timer go stale, not keep it looking alive."""
    import threading

    from bot import competition_actions, competition_timer, heartbeat

    beats: list[str] = []
    monkeypatch.setattr(heartbeat, "record", lambda name, bot: beats.append(name))
    monkeypatch.setattr(competition_timer, "recycle_db_connection", AsyncMock())
    release = threading.Event()

    def steps(enable: bool, actor: str, timer: object = None):
        yield 1
        release.wait(5)
        return CompetitionRunResult(enable=enable)

    monkeypatch.setattr(competition_actions, "competition_steps", steps)
    timer = CompetitionTimer(AsyncMock(spec=discord.Client))

    async def check() -> None:
        await competition_actions.run_competition(True, "timer")

    monkeypatch.setattr(timer, "_check_competition_times", check)
    loop = asyncio.ensure_future(timer.check_loop())
    await asyncio.sleep(0.5)
    wedged = list(beats)
    release.set()
    await loop

    assert wedged == ["timer"]


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_the_timer_clears_a_missed_window_and_says_so() -> None:
    """The bot was down through the whole window: the start can never fire, so the timer drops the schedule."""
    now = timezone.now()
    await CompetitionConfig.objects.aupdate_or_create(
        pk=1,
        defaults={
            "applications_enabled": False,
            "competition_start_time": now - timedelta(hours=9),
            "competition_end_time": now - timedelta(hours=1),
        },
    )
    timer = CompetitionTimer(AsyncMock(spec=discord.Client))
    with (
        patch("bot.competition_timer.run_competition", new_callable=AsyncMock) as run,
        patch("bot.competition_timer.log_to_ops_channel", new_callable=AsyncMock) as ops,
        patch("bot.competition_timer.update_status_channel", new_callable=AsyncMock),
    ):
        await timer._check_competition_times()
        await timer._check_competition_times()

    run.assert_not_awaited()
    config = await CompetitionConfig.objects.aget(pk=1)
    assert (config.competition_start_time, config.competition_end_time) == (None, None)
    ops.assert_awaited_once()
    assert "Schedule cleared" in ops.await_args.args[1]


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_the_timer_clears_a_missed_window_that_ends_before_it_starts() -> None:
    """A pair that ended before it started is a missed window too; clearing it must not fail at every tick."""
    now = timezone.now()
    await CompetitionConfig.objects.aupdate_or_create(
        pk=1,
        defaults={
            "applications_enabled": False,
            "competition_start_time": now - timedelta(hours=1),
            "competition_end_time": now - timedelta(hours=2),
        },
    )
    timer = CompetitionTimer(AsyncMock(spec=discord.Client))
    with (
        patch("bot.competition_timer.log_to_ops_channel", new_callable=AsyncMock),
        patch("bot.competition_timer.update_status_channel", new_callable=AsyncMock),
    ):
        await timer._check_competition_times()

    config = await CompetitionConfig.objects.aget(pk=1)
    assert (config.competition_start_time, config.competition_end_time) == (None, None)


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_an_end_passing_after_a_manual_stop_is_not_called_a_missed_window() -> None:
    now = timezone.now()
    await CompetitionConfig.objects.aupdate_or_create(
        pk=1,
        defaults={
            "applications_enabled": False,
            "competition_start_time": None,
            "competition_end_time": now - timedelta(hours=1),
            "started_this_window": True,
        },
    )
    timer = CompetitionTimer(AsyncMock(spec=discord.Client))
    with (
        patch("bot.competition_timer.log_to_ops_channel", new_callable=AsyncMock) as ops,
        patch("bot.competition_timer.update_status_channel", new_callable=AsyncMock) as status,
    ):
        await timer._check_competition_times()

    assert ops.await_args.args[1] == "**Schedule cleared:** the scheduled end passed after a manual stop."
    status.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_an_end_with_no_start_that_passed_is_a_missed_window() -> None:
    await CompetitionConfig.objects.aupdate_or_create(
        pk=1,
        defaults={
            "applications_enabled": False,
            "competition_start_time": None,
            "competition_end_time": timezone.now() - timedelta(hours=1),
        },
    )
    timer = CompetitionTimer(AsyncMock(spec=discord.Client))
    with (
        patch("bot.competition_timer.log_to_ops_channel", new_callable=AsyncMock) as ops,
        patch("bot.competition_timer.update_status_channel", new_callable=AsyncMock),
    ):
        await timer._check_competition_times()

    assert "would have refused a new start time" in ops.await_args.args[1]


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("enable", [True, False], ids=["start", "stop"])
@pytest.mark.parametrize("confirmed", [False, True], ids=["cancelled", "confirmed"])
async def test_the_start_and_stop_commands_ask_for_confirmation_before_running(
    mock_interaction: Any, enable: bool, confirmed: bool
) -> None:
    from bot.cogs.admin_competition import AdminCompetitionCog

    cog = AdminCompetitionCog(MagicMock())
    command = cog.admin_start_competition if enable else cog.admin_stop_competition
    with (
        patch("bot.cogs.admin_competition.ConfirmView") as view,
        patch("bot.competition_actions.run_competition", new=AsyncMock(return_value=_run(0))) as run,
        patch("bot.cogs.admin_competition.log_to_ops_channel", new_callable=AsyncMock),
        patch("bot.competition_actions.update_status_channel", new_callable=AsyncMock),
    ):
        view.return_value.wait = AsyncMock()
        view.return_value.confirmed = confirmed
        await command.callback(cog, mock_interaction)

    mock_interaction.response.send_message.assert_awaited_once()
    assert run.await_count == (1 if confirmed else 0)


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_timer_queues_1h_and_30m_countdown_announcements_once_each() -> None:
    from core.discord_tasks import BroadcastMessage
    from core.models import DiscordTask
    from core.services.competition import COMPETITION_COUNTDOWN_QUEUED, COUNTDOWN_SENDER

    now = datetime(2026, 10, 3, 23, 0, tzinfo=timezone.UTC)
    end_time = now + timedelta(hours=1)
    await CompetitionConfig.objects.aupdate_or_create(
        pk=1,
        defaults={
            "applications_enabled": True,
            "competition_start_time": None,
            "competition_end_time": end_time,
        },
    )
    timer = CompetitionTimer(AsyncMock(spec=discord.Client))
    with (
        patch("bot.competition_timer.timezone.now", return_value=now - timedelta(seconds=30)) as mock_now,
        patch("bot.competition_timer.log_to_ops_channel", new_callable=AsyncMock) as ops,
    ):
        # 60m30s remaining: not yet in the 1-hour window
        await timer._check_competition_times()
        assert await DiscordTask.objects.acount() == 0

        # 59m remaining: enters 1-hour countdown window; repeat tick does not duplicate
        mock_now.return_value = end_time - timedelta(minutes=59)
        await timer._check_competition_times()
        await timer._check_competition_times()
        assert await DiscordTask.objects.acount() == 1

        # 45m remaining: between milestones, nothing queued
        mock_now.return_value = end_time - timedelta(minutes=45)
        await timer._check_competition_times()
        assert await DiscordTask.objects.acount() == 1

        # 29m remaining: enters 30-minute countdown window; repeat tick does not duplicate
        mock_now.return_value = end_time - timedelta(minutes=29)
        await timer._check_competition_times()
        await timer._check_competition_times()
        assert await DiscordTask.objects.acount() == 2

    broadcasts = [t.typed_payload() async for t in DiscordTask.objects.order_by("pk")]
    assert len(broadcasts) == 2
    assert all(
        isinstance(b, BroadcastMessage) and b.target == "all-teams" and b.sender == COUNTDOWN_SENDER for b in broadcasts
    )
    assert isinstance(broadcasts[0], BroadcastMessage) and "1 hour remaining" in broadcasts[0].message
    assert "Oct 3, 2026, 5:00 PM PDT" in broadcasts[0].message
    assert isinstance(broadcasts[1], BroadcastMessage) and "30 minutes remaining" in broadcasts[1].message
    assert await AuditLog.objects.filter(action=COMPETITION_COUNTDOWN_QUEUED).acount() == 2
    assert ops.await_count == 2
