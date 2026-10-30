"""Blocking work runs off the ORM thread every other bot call waits on: the group refresh and the credentials
resets, which list each team's sessions and run a transaction per team."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from asgiref.sync import sync_to_async

from bot import utils
from bot.cogs.admin_competition import AdminCompetitionCog
from bot.cogs.admin_teams import AdminTeamsCog
from core.authentik_utils import Refused, TeamReset, reset_team, reset_teams_credentials

pytestmark = pytest.mark.asyncio


async def test_the_helper_runs_on_a_pool_thread_and_closes_its_connection():
    flags: list[bool] = []

    def recording(func, *, thread_sensitive=True):
        flags.append(thread_sensitive)
        return sync_to_async(func, thread_sensitive=thread_sensitive)

    connection = MagicMock()
    with patch.dict(utils.off_orm_thread.__globals__, {"sync_to_async": recording, "connection": connection}):
        assert await utils.off_orm_thread(lambda a, b=0: a + b, 2, b=3) == 5

    assert flags == [False]
    connection.close.assert_called_once()


async def test_the_bulk_reset_command_runs_off_the_orm_thread():
    off_thread = AsyncMock(return_value=MagicMock(password_list=[], details=list, failed_resets=[]))
    interaction = MagicMock()
    interaction.response = AsyncMock()
    interaction.followup = AsyncMock()
    callback = AdminCompetitionCog.admin_reset_blueteam_passwords.callback
    names = {"off_orm_thread": off_thread, "settings": MagicMock(AUTHENTIK_TOKEN="t")}
    with patch.dict(callback.__globals__, names):
        await callback(AdminCompetitionCog(MagicMock()), interaction, team_numbers="1")

    assert off_thread.await_args.args[0] is reset_teams_credentials


async def test_the_team_reset_command_runs_off_the_orm_thread():
    off_thread = AsyncMock(return_value=TeamReset(Refused("down"), recreation_queued=False))
    interaction = MagicMock()
    interaction.response = AsyncMock()
    interaction.followup = AsyncMock()
    callback = AdminTeamsCog.admin_reset_team.callback
    team = MagicMock()
    names = {"off_orm_thread": off_thread}
    with (
        patch.dict(callback.__globals__, names),
        patch("bot.cogs.admin_teams.Team.objects.filter", return_value=MagicMock(afirst=AsyncMock(return_value=team))),
    ):
        await callback(AdminTeamsCog(MagicMock()), interaction, team_number=3)

    assert off_thread.await_args.args[0] is reset_team
