"""Tests for competition timer background task."""

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import discord
import pytest
from django.utils import timezone

from bot.competition_timer import CompetitionTimer
from core.models import CompetitionConfig
from core.services.competition import CompetitionRunResult


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
@pytest.mark.django_db(transaction=True)
async def test_repeated_auto_start_failure_is_posted_once() -> None:
    """The timer retries a failed start every minute; ops hears about it once."""
    bot = AsyncMock(spec=discord.Client)
    timer = CompetitionTimer(bot)
    await CompetitionConfig.objects.aupdate_or_create(
        pk=1,
        defaults={"competition_start_time": timezone.now() - timedelta(minutes=1), "applications_enabled": False},
    )
    failure = CompetitionRunResult(enable=True, error="No controlled applications configured")

    with (
        patch("bot.competition_timer.run_competition", new=AsyncMock(return_value=failure)) as start,
        patch("bot.competition_timer.log_to_ops_channel", new_callable=AsyncMock) as ops,
        patch("bot.competition_timer.update_status_channel", new_callable=AsyncMock),
    ):
        await timer._check_competition_times()
        await timer._check_competition_times()

    assert start.await_count == 2
    ops.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("work_fails", [False, True])
async def test_each_pass_recycles_then_beats_only_on_success(work_fails, monkeypatch) -> None:
    """A pass never raises (tasks.loop would stop), and a failed one skips the liveness heartbeat."""
    from bot import competition_timer

    timer = CompetitionTimer(AsyncMock(spec=discord.Client))
    calls: list[str] = []

    async def work() -> None:
        calls.append("work")
        if work_fails:
            raise RuntimeError("db down")

    async def recycle() -> None:
        calls.append("recycle")

    monkeypatch.setattr(competition_timer, "recycle_db_connection", recycle)
    monkeypatch.setattr(competition_timer, "record_heartbeat", lambda name, bot: calls.append(f"beat:{name}"))
    monkeypatch.setattr(timer, "_check_competition_times", work)

    await timer.check_loop()

    assert calls == ["recycle", "work"] if work_fails else ["recycle", "work", "beat:timer"]


@pytest.mark.asyncio
async def test_config_read_failing_on_the_db_skips_the_beat(monkeypatch) -> None:
    """The real work must let a DB error through, or the liveness probe never sees it."""
    from bot import competition_timer

    timer = CompetitionTimer(AsyncMock(spec=discord.Client))
    beats: list[str] = []
    monkeypatch.setattr(competition_timer, "recycle_db_connection", AsyncMock())
    monkeypatch.setattr(competition_timer, "record_heartbeat", lambda name, bot: beats.append(name))

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


def _run(failed_accounts: int) -> CompetitionRunResult:
    result = CompetitionRunResult(enable=True, controlled_apps=["scoring"], apps_ok=["scoring"], accounts_total=3)
    result.accounts_ok, result.accounts_failed = 3 - failed_accounts, failed_accounts
    return result


async def _start_due() -> None:
    await CompetitionConfig.objects.aupdate_or_create(
        pk=1,
        defaults={"competition_start_time": timezone.now() - timedelta(minutes=1), "applications_enabled": False},
    )


def _fake_runs(failures: list[int], stop_by_hand_after: int | None = None):
    """Stand-in for run_competition: records the state like the real run and beats once per attempt."""
    calls = iter(failures)
    attempt = 0

    async def run(enable: bool, actor: str, on_step=None) -> CompetitionRunResult:
        nonlocal attempt
        attempt += 1
        await CompetitionConfig.objects.filter(pk=1).aupdate(applications_enabled=enable)
        if on_step:
            on_step()
        if stop_by_hand_after == attempt:
            await CompetitionConfig.objects.filter(pk=1).aupdate(applications_enabled=not enable)
        return _run(next(calls))

    return run


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("runs", "expected_note"),
    [
        ([1, 0], "Completed on attempt 2."),
        ([2, 2, 1, 1], "Still incomplete after 4 attempts: run Start again from the competition page."),
    ],
)
async def test_an_incomplete_auto_start_is_retried_up_to_three_times(runs, expected_note) -> None:
    """A partial start (Authentik timing out at 16:00) is run again, a bounded number of times."""
    await _start_due()
    timer = CompetitionTimer(AsyncMock(spec=discord.Client))
    with (
        patch("bot.competition_timer.run_competition", new=AsyncMock(side_effect=_fake_runs(runs))) as start,
        patch("bot.competition_timer.asyncio.sleep", new_callable=AsyncMock) as sleep,
        patch("bot.competition_timer.record_heartbeat") as beat,
        patch("bot.competition_timer.log_to_ops_channel", new_callable=AsyncMock) as ops,
        patch("bot.competition_timer.update_status_channel", new_callable=AsyncMock),
    ):
        await timer._check_competition_times()

    assert start.await_count == len(runs)
    assert sleep.await_count == len(runs) - 1
    # Every attempt passes a step callback that beats the timer's heartbeat
    assert beat.call_count == len(runs)
    ops.assert_awaited_once()
    assert ops.await_args.args[1].endswith(expected_note)


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_a_stop_by_hand_during_the_retry_wait_is_not_undone() -> None:
    """The next attempt would re-enable everything an operator had just stopped."""
    await _start_due()
    timer = CompetitionTimer(AsyncMock(spec=discord.Client))
    with (
        patch(
            "bot.competition_timer.run_competition",
            new=AsyncMock(side_effect=_fake_runs([2, 2, 2, 2], stop_by_hand_after=1)),
        ) as start,
        patch("bot.competition_timer.asyncio.sleep", new_callable=AsyncMock),
        patch("bot.competition_timer.record_heartbeat"),
        patch("bot.competition_timer.log_to_ops_channel", new_callable=AsyncMock) as ops,
        patch("bot.competition_timer.update_status_channel", new_callable=AsyncMock),
    ):
        await timer._check_competition_times()

    assert start.await_count == 1
    assert not (await CompetitionConfig.objects.aget(pk=1)).applications_enabled
    assert ops.await_args.args[1].endswith("retries stopped: the competition was stopped by hand.")


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_a_complete_auto_start_runs_once() -> None:
    await _start_due()
    timer = CompetitionTimer(AsyncMock(spec=discord.Client))
    with (
        patch("bot.competition_timer.run_competition", new=AsyncMock(side_effect=_fake_runs([0]))) as start,
        patch("bot.competition_timer.asyncio.sleep", new_callable=AsyncMock) as sleep,
        patch("bot.competition_timer.log_to_ops_channel", new_callable=AsyncMock) as ops,
        patch("bot.competition_timer.update_status_channel", new_callable=AsyncMock),
    ):
        await timer._check_competition_times()

    start.assert_awaited_once()
    sleep.assert_not_awaited()
    assert "attempt" not in ops.await_args.args[1]


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
