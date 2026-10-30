"""Stopping the competition disables the team accounts; their members keep the team's Discord roles until the team
itself is deactivated, but the portal grants a disabled account nothing."""

from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth.models import User

from bot.role_sync import role_grants
from core.auth_utils import has_permission, permission_groups_of
from core.models import CompetitionConfig, UserGroups
from core.services.competition import run_competition_to_completion
from core.services.user_groups import refresh_user_groups
from team.models import DiscordLink, Team

pytestmark = pytest.mark.django_db

BLUETEAM = 8001
TEAM_ROLE = 8002
OPS = 8003


@pytest.fixture(autouse=True)
def discord_settings(settings):
    settings.BLUETEAM_ROLE_ID = BLUETEAM
    settings.GROUP_ROLE_MAPPING = {"WCComps_Ops": OPS}


def _authentik(account_active: bool, groups: list[str]) -> MagicMock:
    manager = MagicMock()
    manager.__enter__.return_value = manager
    manager.find_user.return_value = {"pk": 1, "username": "team01", "uid": "sub-01"}
    manager.list_all_groups.return_value = [
        {"pk": "g-team", "name": "WCComps_BlueTeam01", "parents": []},
        {"pk": "g-ops", "name": "WCComps_Ops", "parents": []},
    ]
    manager.list_all_users.return_value = [{"uid": "sub-01", "is_active": account_active, "groups": groups}]
    return manager


@pytest.fixture
def member() -> User:
    team = Team.objects.create(team_number=1, team_name="Team 01", discord_role_id=TEAM_ROLE)
    account = User.objects.create(username="team01")
    UserGroups.objects.create(user=account, authentik_id="sub-01", groups=["WCComps_BlueTeam01", "WCComps_Ops"])
    DiscordLink.objects.create(user=account, discord_id=77, discord_username="m", team=team, is_active=True)
    CompetitionConfig.objects.update_or_create(
        pk=1, defaults={"controlled_applications": ["scoring"], "applications_enabled": True}
    )
    return account


def test_stopping_keeps_the_team_roles_and_takes_the_portal_away(member):
    manager = _authentik(account_active=False, groups=["g-team", "g-ops"])
    with (
        patch("core.authentik_manager.AuthentikManager", return_value=manager),
        patch("scoring.quotient_sync.sync_service_scores"),
    ):
        result = run_competition_to_completion(False, "gold-admin")

    assert result.accounts_ok == result.accounts_total
    assert role_grants().by_member[77] == frozenset({TEAM_ROLE, BLUETEAM})
    member.refresh_from_db()
    assert permission_groups_of(member) == []
    assert not has_permission(member, "blue_team")


def test_a_disabled_account_removed_from_the_team_group_loses_the_team_roles(member):
    refresh_user_groups(_authentik(account_active=False, groups=["g-ops"]))

    assert role_grants().by_member[77] == frozenset()


def test_a_deactivated_team_takes_the_roles_of_a_disabled_account(member):
    refresh_user_groups(_authentik(account_active=False, groups=["g-team"]))
    Team.objects.filter(team_number=1).update(is_active=False)

    assert role_grants().by_member[77] == frozenset()


def test_role_grants_reads_every_link_in_two_queries(member, django_assert_num_queries):
    """The seat is read from the loaded link, not queried per member."""
    team = Team.objects.get(team_number=1)
    for n in range(5):
        account = User.objects.create(username=f"member{n}")
        UserGroups.objects.create(user=account, authentik_id=f"sub-m{n}", groups=["WCComps_BlueTeam01"])
        DiscordLink.objects.create(user=account, discord_id=100 + n, discord_username=f"m{n}", team=team)

    with django_assert_num_queries(2):
        grants = role_grants()

    assert grants.by_member[100] == frozenset({TEAM_ROLE, BLUETEAM})
