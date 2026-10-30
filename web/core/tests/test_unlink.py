"""Ending Discord links: one operation, writing the same audit row for every caller."""

import re

import pytest
from django.contrib.auth.models import User

from core.models import AuditLog, DiscordTask
from core.services.linking import unlink_discord_links
from core.tests.guards import REPO_DIR, fail_with, tracked_files
from team.models import DiscordLink, Team

pytestmark = pytest.mark.django_db


def _link(discord_id: int, team: Team | None, *, active: bool = True) -> DiscordLink:
    user = User.objects.create(username=f"account{discord_id}")
    return DiscordLink.objects.create(
        discord_id=discord_id, discord_username=f"member{discord_id}", user=user, team=team, is_active=active
    )


def test_unlinking_ends_each_link_audits_it_and_queues_its_role_sync():
    team = Team.objects.create(team_number=3, team_name="Team 03")
    first, second = _link(31, team), _link(32, team)

    ended = unlink_discord_links(DiscordLink.objects.filter(team=team), actor="gold-admin", reason="team_reset")

    assert sorted(link.discord_id for link in ended) == [31, 32]
    for link in (first, second):
        link.refresh_from_db()
        assert not link.is_active and link.unlinked_at is not None
    rows = AuditLog.objects.filter(action="user_unlinked").order_by("target_id")
    assert [(r.admin_user, r.target_entity, r.target_id, r.details) for r in rows] == [
        (
            "gold-admin",
            "discord_link",
            n,
            {
                "discord_id": n,
                "discord_username": f"member{n}",
                "team_name": "Team 03",
                "authentik_username": f"account{n}",
                "reason": "team_reset",
            },
        )
        for n in (31, 32)
    ]
    syncs = DiscordTask.objects.filter(task_type="sync_member_roles").order_by("id")
    assert [t.payload for t in syncs] == [{"discord_id": 31}, {"discord_id": 32}]


def test_ended_links_are_left_alone():
    _link(41, None, active=False)

    assert unlink_discord_links(DiscordLink.objects.all(), actor="gold-admin", reason="admin_unlink") == []
    assert not AuditLog.objects.exists()
    assert not DiscordTask.objects.exists()


def test_a_caller_syncing_the_whole_guild_after_can_skip_the_per_member_syncs():
    _link(51, Team.objects.create(team_number=5, team_name="Team 05"))

    unlink_discord_links(DiscordLink.objects.all(), actor="ops", reason="competition_cleanup", sync_roles=False)

    assert not DiscordTask.objects.filter(task_type="sync_member_roles").exists()
    assert AuditLog.objects.filter(action="user_unlinked").count() == 1


UNLINK_CODE = re.compile(r"\bunlinked_at\s*=|[\"']user_unlinked[\"']")

# "path:line" (or "path:*") -> why that code ends a link itself
EXCEPTIONS: dict[str, str] = {
    "web/core/services/linking.py:*": "unlink_discord_links itself",
    "web/team/models.py:*": "the field, and deactivate_previous_links replacing a link with a new one",
}


def test_links_end_only_through_unlink_discord_links():
    found = {}
    for name in tracked_files():
        if not name.endswith(".py") or "/tests/" in name or "/migrations/" in name:
            continue
        for number, line in enumerate((REPO_DIR / name).read_text().splitlines(), 1):
            if UNLINK_CODE.search(line.split("#")[0]):
                found[f"{name}:{number}"] = line.strip()
    fail_with("A Discord link ended by hand (use core.services.linking.unlink_discord_links)", found, EXCEPTIONS)
