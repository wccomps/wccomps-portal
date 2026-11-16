"""arun_competition: run_competition from async code, the timer's and the bot's commands' entry point."""

from unittest.mock import patch

import pytest
from asgiref.sync import sync_to_async

from core.models import CompetitionConfig
from core.services.competition import CompetitionRunResult, arun_competition


@pytest.mark.parametrize("enable", [True, False])
async def test_runs_the_shared_service(enable: bool) -> None:
    done = CompetitionRunResult(enable=enable)
    calls: list[tuple[bool, str]] = []

    def steps(enable: bool, actor: str, timer: object = None):
        calls.append((enable, actor))
        yield "step"
        return done

    with patch("core.services.competition.run_competition", steps):
        result = await arun_competition(enable, actor="discord:admin")

    assert result is done
    assert calls == [(enable, "discord:admin")]


@pytest.mark.django_db(transaction=True)
async def test_a_connection_broken_between_runs_does_not_fail_the_next() -> None:
    """The run's pool thread keeps no connection across runs, so a database restart between start and stop
    can't leave every later timer run failing on a dead one."""
    from django.db import connection

    backends: list[int] = []

    def read_config_and_backend(enable: bool, actor: str, timer: object = None):
        yield "step"
        CompetitionConfig.get_config()
        with connection.cursor() as cursor:
            cursor.execute("select pg_backend_pid()")
            backends.append(cursor.fetchone()[0])
        return CompetitionRunResult(enable=enable)

    with patch("core.services.competition.run_competition", read_config_and_backend):
        await arun_competition(True, actor="timer")
        backend = backends[0]

        def terminate() -> None:
            with connection.cursor() as cursor:
                cursor.execute("select pg_terminate_backend(%s)", [backend])

        await sync_to_async(terminate)()

        for _ in range(3):
            await arun_competition(False, actor="timer")
