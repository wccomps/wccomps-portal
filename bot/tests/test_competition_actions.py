"""The bot's entry point to starting and stopping the competition."""

from unittest.mock import patch

import pytest

from bot.competition_actions import run_competition
from core.services.competition import CompetitionRunResult


@pytest.mark.asyncio
@pytest.mark.parametrize("enable", [True, False])
async def test_runs_the_shared_service(enable: bool) -> None:
    done = CompetitionRunResult(enable=enable)
    calls: list[tuple[bool, str]] = []

    def steps(enable: bool, actor: str, timer: object = None):
        calls.append((enable, actor))
        yield "step"
        return done

    with patch("bot.competition_actions.competition_steps", steps):
        result = await run_competition(enable, actor="discord:admin")

    assert result is done
    assert calls == [(enable, "discord:admin")]


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_a_connection_broken_between_runs_does_not_fail_the_next() -> None:
    """The run's pool thread keeps no connection across runs, so a database restart between start and stop
    can't leave every later timer run failing on a dead one."""
    from asgiref.sync import sync_to_async
    from django.db import connection

    from core.models import CompetitionConfig

    backends: list[int] = []

    def read_config_and_backend(enable: bool, actor: str, timer: object = None):
        yield "step"
        CompetitionConfig.get_config()
        with connection.cursor() as cursor:
            cursor.execute("select pg_backend_pid()")
            backends.append(cursor.fetchone()[0])
        return CompetitionRunResult(enable=enable)

    with patch("bot.competition_actions.competition_steps", read_config_and_backend):
        await run_competition(True, actor="timer")
        backend = backends[0]

        def terminate() -> None:
            with connection.cursor() as cursor:
                cursor.execute("select pg_terminate_backend(%s)", [backend])

        await sync_to_async(terminate)()

        for _ in range(3):
            await run_competition(False, actor="timer")


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(("command", "time"), [("start", "2026-10-04T01:00"), ("end", "2026-10-03T15:00")])
async def test_schedule_commands_refuse_an_end_before_the_start(mock_interaction, mock_bot, command, time) -> None:
    from datetime import UTC, datetime

    from bot.cogs.admin_competition import AdminCompetitionCog
    from core.models import CompetitionConfig

    start, end = datetime(2026, 10, 3, 16, tzinfo=UTC), datetime(2026, 10, 4, tzinfo=UTC)
    await CompetitionConfig.objects.aupdate_or_create(
        pk=1, defaults={"competition_start_time": start, "competition_end_time": end}
    )
    cog = AdminCompetitionCog(mock_bot)
    callback = getattr(cog, f"admin_set_{command}_time").callback

    await callback(cog, mock_interaction, datetime_str=time, timezone_name="UTC")

    mock_interaction.response.send_message.assert_called_once_with(
        "The end time must be after the start time", ephemeral=True
    )
    config = await CompetitionConfig.objects.aget(pk=1)
    assert (config.competition_start_time, config.competition_end_time) == (start, end)
