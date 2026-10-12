"""The live-data check covers exactly what the competition wipe deletes."""

import pytest
from django.contrib.auth.models import User

from core.competition_utils import live_competition_data_exists, wipe_competition_data
from team.models import DiscordLink, Team

pytestmark = pytest.mark.django_db


def test_nothing_left_means_no_live_data():
    Team.objects.create(team_number=1, team_name="Team 01")

    assert not live_competition_data_exists()


def test_a_team_discord_link_is_live_data_until_the_wipe():
    team = Team.objects.create(team_number=1, team_name="Team 01")
    user = User.objects.create_user(username="member")
    DiscordLink.objects.create(discord_id=1, discord_username="member", user=user, team=team, is_active=True)

    assert live_competition_data_exists()
    wipe_competition_data()
    assert not live_competition_data_exists()


def test_a_staff_discord_link_is_not_live_data():
    user = User.objects.create_user(username="staff")
    DiscordLink.objects.create(discord_id=2, discord_username="staff", user=user, is_active=True)

    assert not live_competition_data_exists()


def test_an_orange_check_in_is_live_data():
    from orange_team.models import OrangeCheckIn

    OrangeCheckIn.objects.create(user=User.objects.create_user(username="orange"))

    assert live_competition_data_exists()
