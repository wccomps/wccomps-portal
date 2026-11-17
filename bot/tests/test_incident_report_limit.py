"""/incident-report is held to the same per-team submission limit as the web form: past it nothing is saved and the
reporter gets a fixed reply; staff filing for a team aren't limited."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from django.contrib.auth.models import User
from django.utils import timezone
from scoring.models import IncidentReport

from bot.cogs.scoring import ScoringCog
from core.models import UserGroups
from team.models import Team

DESCRIPTION = "We saw a SQL injection in the web logs and blocked the address at the firewall right away."


async def _file(team: Team, user: User) -> MagicMock:
    interaction = MagicMock()
    interaction.response.send_message = AsyncMock()
    member = SimpleNamespace(team=team, user=user)
    with patch("bot.cogs.scoring.linked_team_member", AsyncMock(return_value=member)):
        await ScoringCog.incident_report.callback(
            ScoringCog(MagicMock()), interaction, "web", "http", "10.0.0.5", "SQL injection", DESCRIPTION
        )
    return interaction


async def _filed(team: Team, user: User, count: int) -> None:
    for _ in range(count):
        await IncidentReport.objects.acreate(
            team=team,
            submitted_by=user,
            attack_description="Seen",
            source_ip="10.0.0.5",
            attack_detected_at=timezone.now(),
        )


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_a_report_past_the_teams_limit_is_refused_and_not_saved() -> None:
    team = await Team.objects.acreate(team_number=7, team_name="Team 07", authentik_group="WCComps_BlueTeam07")
    user = await User.objects.acreate(username="member07")
    await UserGroups.objects.acreate(user=user, authentik_id="member07-uid", groups=[team.authentik_group])
    await _filed(team, user, IncidentReport.SUBMISSION_LIMIT)

    interaction = await _file(team, user)

    assert await IncidentReport.objects.filter(team=team).acount() == IncidentReport.SUBMISSION_LIMIT
    interaction.response.send_message.assert_awaited_once_with(IncidentReport.limit_reached_message(), ephemeral=True)


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_a_report_under_the_limit_is_saved() -> None:
    team = await Team.objects.acreate(team_number=7, team_name="Team 07", authentik_group="WCComps_BlueTeam07")
    user = await User.objects.acreate(username="member07")
    await UserGroups.objects.acreate(user=user, authentik_id="member07-uid", groups=[team.authentik_group])
    await _filed(team, user, IncidentReport.SUBMISSION_LIMIT - 1)

    await _file(team, user)

    assert await IncidentReport.objects.filter(team=team).acount() == IncidentReport.SUBMISSION_LIMIT


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_staff_filing_for_a_team_arent_limited() -> None:
    team = await Team.objects.acreate(team_number=7, team_name="Team 07", authentik_group="WCComps_BlueTeam07")
    staff = await User.objects.acreate(username="whiteteam")
    await UserGroups.objects.acreate(user=staff, authentik_id="white-uid", groups=["WCComps_WhiteTeam"])
    await _filed(team, staff, IncidentReport.SUBMISSION_LIMIT)

    await _file(team, staff)

    assert await IncidentReport.objects.filter(team=team).acount() == IncidentReport.SUBMISSION_LIMIT + 1
