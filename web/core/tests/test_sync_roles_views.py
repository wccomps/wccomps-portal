"""Sync Roles page: dry and live runs can be queued, and results include the per-person list."""

import pytest
from django.test import Client
from django.urls import reverse

from core.models import DiscordTask

pytestmark = pytest.mark.django_db


@pytest.fixture
def admin_client(admin_user):
    client = Client()
    client.force_login(admin_user)
    return client


@pytest.mark.parametrize(
    ("posted", "expected_dry_run"), [({"dry_run": "false"}, False), ({"dry_run": "true"}, True), ({}, True)]
)
def test_action_honors_dry_run_and_defaults_to_preview(admin_client, posted, expected_dry_run):
    response = admin_client.post(reverse("admin_sync_roles_action"), posted)

    assert response.status_code == 200
    task = DiscordTask.objects.get(pk=response.json()["task_id"])
    assert task.payload["dry_run"] is expected_dry_run


def test_status_returns_summary_and_changes(admin_client):
    task = DiscordTask.objects.create(
        task_type="sync_roles",
        status="completed",
        payload={"requested_by": "admin", "dry_run": True},
        result={
            "roles_added": 2,
            "roles_removed": 1,
            "errors": 0,
            "changes": ["[DRY RUN] Removed Gold Team from bob (Bob) (not linked)"],
        },
    )

    data = admin_client.get(reverse("admin_task_status", args=[task.pk])).json()

    assert "2 would be added" in data["message"]
    assert "1 would be removed" in data["message"]
    assert data["changes"] == task.result["changes"]


def test_blue_team_cannot_trigger_sync(blue_team_user):
    client = Client()
    client.force_login(blue_team_user)

    response = client.post(reverse("admin_sync_roles_action"), {"dry_run": "false"})

    assert response.status_code == 403
    assert not DiscordTask.objects.filter(task_type="sync_roles").exists()
