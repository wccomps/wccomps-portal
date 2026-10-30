"""The bot's entry point to starting and stopping the competition."""

from unittest.mock import patch

import pytest

from bot.competition_actions import run_competition
from core.services.competition import CompetitionRunResult


@pytest.mark.asyncio
@pytest.mark.parametrize("enable", [True, False])
async def test_runs_the_shared_service(enable: bool) -> None:
    done = CompetitionRunResult(enable=enable)
    with patch("bot.competition_actions.run_competition_to_completion", return_value=done) as service:
        result = await run_competition(enable, actor="discord:admin")

    assert result is done
    service.assert_called_once_with(enable, "discord:admin", None)


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_a_connection_broken_between_runs_does_not_fail_the_next() -> None:
    """The run's pool thread keeps no connection across runs, so a database restart between start and stop
    can't leave every later timer run failing on a dead one."""
    from asgiref.sync import sync_to_async
    from django.db import connection

    from core.models import CompetitionConfig

    def read_config_and_backend(enable: bool, actor: str, on_step=None) -> int:
        CompetitionConfig.get_config()
        with connection.cursor() as cursor:
            cursor.execute("select pg_backend_pid()")
            return cursor.fetchone()[0]

    with patch("bot.competition_actions.run_competition_to_completion", side_effect=read_config_and_backend):
        backend = await run_competition(True, actor="timer")

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

    mock_interaction.followup.send.assert_called_once_with("The end time must be after the start time", ephemeral=True)
    config = await CompetitionConfig.objects.aget(pk=1)
    assert (config.competition_start_time, config.competition_end_time) == (start, end)


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_set_apps_keeps_a_start_the_timer_recorded_meanwhile(mock_interaction, mock_bot) -> None:
    """set-apps loads the config, then the timer records its start: saving the stale copy would mark the competition
    stopped and bring back the consumed start time, so the timer would start it again and resend the start message."""
    from datetime import timedelta

    from asgiref.sync import sync_to_async
    from django.utils import timezone

    from bot.cogs.admin_competition import AdminCompetitionCog
    from core.models import CompetitionConfig

    await CompetitionConfig.objects.aupdate_or_create(
        pk=1, defaults={"applications_enabled": False, "competition_start_time": timezone.now() - timedelta(minutes=1)}
    )
    stale = await sync_to_async(CompetitionConfig.get_config)()
    await CompetitionConfig.objects.filter(pk=1).aupdate(applications_enabled=True, competition_start_time=None)
    cog = AdminCompetitionCog(mock_bot)

    with (
        patch.object(CompetitionConfig, "get_config", return_value=stale),
        patch("bot.cogs.admin_competition.log_to_ops_channel"),
    ):
        await cog.admin_competition_set_apps.callback(cog, mock_interaction, app_slugs="scoring")

    config = await CompetitionConfig.objects.aget(pk=1)
    assert (config.applications_enabled, config.competition_start_time) == (True, None)
    assert config.controlled_applications == ["scoring"]


@pytest.mark.parametrize("command", ["admin_set_start_time", "admin_set_end_time"])
def test_the_schedule_commands_offer_the_setup_pages_time_zones(command) -> None:
    from bot.cogs.admin_competition import AdminCompetitionCog
    from core.utils import TIMEZONE_CHOICES

    parameter = next(p for p in getattr(AdminCompetitionCog, command).parameters if p.name == "timezone_name")
    assert [(choice.value, choice.name) for choice in parameter.choices] == TIMEZONE_CHOICES


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(("command", "field"), [("start", "start_time"), ("end", "end_time")])
async def test_schedule_commands_record_what_the_setup_page_records(mock_interaction, mock_bot, command, field) -> None:
    from bot.cogs.admin_competition import AdminCompetitionCog
    from core.models import AuditLog

    cog = AdminCompetitionCog(mock_bot)
    with patch("bot.cogs.admin_competition.log_to_ops_channel"):
        await getattr(cog, f"admin_set_{command}_time").callback(
            cog, mock_interaction, datetime_str="2099-10-03T16:00", timezone_name="UTC"
        )

    audit = await AuditLog.objects.aget(action="competition_schedule_set")
    assert audit.details[field] == "2099-10-03T16:00:00+00:00"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_set_max_members_marks_the_config_updated(mock_interaction, mock_bot) -> None:
    from datetime import UTC, datetime

    from bot.cogs.admin_competition import AdminCompetitionCog
    from core.models import CompetitionConfig

    long_ago = datetime(2020, 1, 1, tzinfo=UTC)
    await CompetitionConfig.objects.aupdate_or_create(pk=1, defaults={})
    await CompetitionConfig.objects.filter(pk=1).aupdate(updated_at=long_ago)
    cog = AdminCompetitionCog(mock_bot)
    with patch("bot.cogs.admin_competition.log_to_ops_channel"):
        await cog.admin_set_max_members.callback(cog, mock_interaction, max_members=7)

    config = await CompetitionConfig.objects.aget(pk=1)
    assert config.max_team_members == 7
    assert config.updated_at > long_ago


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_clear_start_time_cancels_the_scheduled_start(mock_interaction, mock_bot) -> None:
    from datetime import UTC, datetime

    from bot.cogs.admin_competition import AdminCompetitionCog
    from core.models import AuditLog, CompetitionConfig

    end = datetime(2099, 10, 4, tzinfo=UTC)
    await CompetitionConfig.objects.aupdate_or_create(
        pk=1, defaults={"competition_start_time": datetime(2099, 10, 3, tzinfo=UTC), "competition_end_time": end}
    )
    cog = AdminCompetitionCog(mock_bot)
    with patch("bot.cogs.admin_competition.log_to_ops_channel"):
        await cog.admin_clear_start_time.callback(cog, mock_interaction)

    config = await CompetitionConfig.objects.aget(pk=1)
    assert (config.competition_start_time, config.competition_end_time) == (None, end)
    assert (await AuditLog.objects.aget(action="competition_schedule_set")).details["start_time"] is None
    mock_interaction.followup.send.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_clear_start_time_works_on_a_stored_schedule_that_ends_before_it_starts(
    mock_interaction, mock_bot
) -> None:
    from datetime import UTC, datetime

    from bot.cogs.admin_competition import AdminCompetitionCog
    from core.models import CompetitionConfig

    await CompetitionConfig.objects.aupdate_or_create(
        pk=1,
        defaults={
            "competition_start_time": datetime(2099, 10, 4, tzinfo=UTC),
            "competition_end_time": datetime(2099, 10, 3, tzinfo=UTC),
        },
    )
    cog = AdminCompetitionCog(mock_bot)
    with patch("bot.cogs.admin_competition.log_to_ops_channel"):
        await cog.admin_clear_start_time.callback(cog, mock_interaction)

    assert (await CompetitionConfig.objects.aget(pk=1)).competition_start_time is None
    mock_interaction.followup.send.assert_awaited_once()
