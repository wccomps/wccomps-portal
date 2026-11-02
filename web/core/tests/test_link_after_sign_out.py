"""A link confirmed by a session signed out since its request began (a credentials reset) is refused, so it can't
outlast the reset's unlink. The request's user is the one loaded with that session."""

import pytest
from django.contrib.auth.models import User

from core.auth_utils import sign_out_everywhere
from core.services.linking import LinkResult, execute_link
from team.models import DiscordLink, Team

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("on_team", [True, False])
def test_a_session_signed_out_while_linking_links_nothing(on_team):
    team = Team.objects.create(team_number=9, team_name="Team 09") if on_team else None
    account = User.objects.create(username="team09")
    sign_out_everywhere(User.objects.get(pk=account.pk))

    result = execute_link(909, "member", account, team)

    assert isinstance(result, LinkResult) and not result.success
    assert not DiscordLink.objects.exists()


def test_a_session_still_signed_in_links():
    team = Team.objects.create(team_number=9, team_name="Team 09")
    account = User.objects.create(username="team09")

    assert execute_link(909, "member", account, team) is None
    assert DiscordLink.objects.filter(discord_id=909, team=team, is_active=True).exists()
