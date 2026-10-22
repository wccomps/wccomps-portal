"""Linking queues the member's role sync, after setting up an active team's Discord role and channels."""

from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from core.models import DiscordTask
from core.services.linking import finalize_link
from team.models import LinkToken, Team

pytestmark = pytest.mark.django_db


def _link(team: Team | None) -> list[tuple[str, dict[str, object]]]:
    User.objects.create(username="linker")
    token = LinkToken.objects.create(
        token="t" * 32, discord_id=4242, discord_username="linker", expires_at=timezone.now() + timedelta(minutes=5)
    )
    finalize_link(token, 4242, "linker", "linker", team)
    return [
        (t.task_type, t.payload)
        for t in DiscordTask.objects.order_by("created_at", "id")
        if t.task_type != "log_to_channel"
    ]


def test_active_team_is_set_up_before_the_member_is_synced():
    team = Team.objects.create(team_number=9, team_name="Team 09")

    assert _link(team) == [
        ("setup_team_infrastructure", {"team_number": 9, "recreate": False}),
        ("sync_member_roles", {"discord_id": 4242}),
    ]


def test_inactive_team_only_syncs_the_member():
    """The sync grants nothing for an inactive team's seat, so there's nothing to set up."""
    team = Team.objects.create(team_number=9, team_name="Team 09", is_active=False)

    assert _link(team) == [("sync_member_roles", {"discord_id": 4242})]


def test_non_team_link_only_syncs_the_member():
    assert _link(None) == [("sync_member_roles", {"discord_id": 4242})]
