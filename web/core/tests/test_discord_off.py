"""With no Discord bot token, the Discord-only screens, actions and checks are gone."""

import pytest
from django.urls import reverse

from core.admin_views import readiness
from core.models import QueuedTask
from team.models import Team

pytestmark = pytest.mark.django_db


@pytest.fixture
def discord_off(settings):
    settings.DISCORD_BOT_TOKEN = ""


@pytest.fixture
def team():
    return Team.objects.create(team_number=3, team_name="Team 3", is_active=True)


def test_sync_roles_404_when_off(admin_client, discord_off):
    assert admin_client.get(reverse("admin_sync_roles")).status_code == 404
    assert admin_client.post(reverse("admin_sync_roles_action"), {"dry_run": "on"}).status_code == 404
    assert not QueuedTask.objects.exists()


def test_recreate_actions_404_when_off(admin_client, discord_off, team):
    url = reverse("admin_team_action", args=[team.team_number])
    assert admin_client.post(url, {"action": "recreate_channels"}).status_code == 404
    bulk = admin_client.post(
        reverse("admin_teams_bulk_action"), {"action": "recreate", "team_numbers": str(team.team_number)}
    )
    assert bulk.status_code == 404
    assert not QueuedTask.objects.exists()


def test_other_team_actions_still_work_when_off(admin_client, discord_off, team):
    url = reverse("admin_team_action", args=[team.team_number])
    assert admin_client.post(url, {"action": "deactivate"}).status_code == 200
    team.refresh_from_db()
    assert not team.is_active


def test_readiness_skips_discord_check_when_off(discord_off):
    assert "Discord IDs configured" not in [name for name, _ in readiness.checks()]


def test_readiness_keeps_discord_check_when_on():
    assert "Discord IDs configured" in [name for name, _ in readiness.checks()]


def test_nav_hides_sync_roles_when_off(admin_client, discord_off):
    assert reverse("admin_sync_roles") not in admin_client.get(reverse("admin_teams")).content.decode()


def test_discord_screens_shown_when_on(admin_client):
    assert reverse("admin_sync_roles") in admin_client.get(reverse("admin_teams")).content.decode()


def test_team_pages_hide_discord_bits_when_off(admin_client, discord_off, team):
    teams = admin_client.get(reverse("admin_teams")).content.decode()
    assert 'value="recreate"' not in teams
    assert 'name="discord"' not in teams
    detail = admin_client.get(reverse("admin_team_detail", args=[team.team_number])).content.decode()
    assert "recreate_channels" not in detail
    assert "Linked Members" not in detail


def test_team_pages_show_discord_bits_when_on(admin_client, team):
    teams = admin_client.get(reverse("admin_teams")).content.decode()
    assert 'value="recreate"' in teams
    detail = admin_client.get(reverse("admin_team_detail", args=[team.team_number])).content.decode()
    assert "recreate_channels" in detail
    assert "Linked Members" in detail
