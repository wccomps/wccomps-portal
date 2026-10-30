"""A member's managed roles follow what their active DiscordLink grants; everyone else loses them."""

import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from django.contrib.auth.models import User

from bot import role_sync
from bot.cogs import authentik_groups
from core.models import UserGroups
from team.models import DiscordLink, Team

pytestmark = [pytest.mark.asyncio, pytest.mark.django_db(transaction=True)]

GUILD_ID = 7001
BLUETEAM = 7002
TEAM_A = 7003
TEAM_B = 7004
OPS = 7005
BOSS = 7006  # above the bot's top role
ALL_ROLES = {BLUETEAM, TEAM_A, TEAM_B, OPS, BOSS}


def _role(role_id: int) -> SimpleNamespace:
    return SimpleNamespace(id=role_id, name=f"role{role_id}", is_assignable=lambda: role_id != BOSS)


def _member(member_id: int, *role_ids: int, bot: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        id=member_id,
        bot=bot,
        name=f"user{member_id}",
        display_name=f"User {member_id}",
        roles=[_role(r) for r in role_ids],
        add_roles=AsyncMock(),
        remove_roles=AsyncMock(),
    )


def _guild(*members: SimpleNamespace) -> MagicMock:
    roles = {role_id: _role(role_id) for role_id in ALL_ROLES}
    guild = MagicMock()
    guild.chunked = True
    guild.me.guild_permissions.manage_roles = True
    guild.members = list(members)
    guild.get_role.side_effect = roles.get
    return guild


@pytest.fixture(autouse=True)
def discord_settings(settings: Any) -> None:
    settings.COMPETITION_GUILD_ID = GUILD_ID
    settings.BLUETEAM_ROLE_ID = BLUETEAM
    settings.GROUP_ROLE_MAPPING = {"WCComps_Ops": OPS}


@pytest.fixture
async def teams() -> tuple[Team, Team]:
    a = await Team.objects.acreate(team_number=5, team_name="Team 05", discord_role_id=TEAM_A)
    b = await Team.objects.acreate(team_number=6, team_name="Team 06", discord_role_id=TEAM_B)
    return a, b


async def _link(discord_id: int, groups: list[str], team: Team | None = None) -> DiscordLink:
    user = await User.objects.acreate(username=f"u{discord_id}")
    await UserGroups.objects.acreate(user=user, authentik_id=f"uid{discord_id}", groups=groups)
    return await DiscordLink.objects.acreate(
        discord_id=discord_id, discord_username=user.username, user=user, team=team, is_active=True
    )


async def _sync(guild: MagicMock, *, dry_run: bool = False) -> dict[str, Any]:
    return dict(await role_sync.sync_roles(guild, dry_run=dry_run))


def _ids(mock: AsyncMock) -> set[int]:
    return {role.id for call in mock.await_args_list for role in call.args}


async def test_linking_before_joining_gets_the_roles_on_the_first_pass_after_joining(teams) -> None:
    await _link(1, ["WCComps_Ops"], team=teams[0])
    assert (await _sync(_guild()))["roles_added"] == 0

    member = _member(1)
    stats = await _sync(_guild(member))

    assert _ids(member.add_roles) == {OPS, TEAM_A, BLUETEAM}
    assert stats["roles_added"] == 3


async def test_a_member_holding_exactly_their_grant_is_left_alone(teams) -> None:
    await _link(1, ["WCComps_Ops"], team=teams[0])
    member = _member(1, OPS, TEAM_A, BLUETEAM)

    await _sync(_guild(member))

    member.add_roles.assert_not_awaited()
    member.remove_roles.assert_not_awaited()


async def test_linked_member_outside_the_group_loses_the_mapped_role(teams) -> None:
    await _link(1, [])
    member = _member(1, OPS)

    stats = await _sync(_guild(member))

    assert _ids(member.remove_roles) == {OPS}
    assert "not granted by their Authentik groups or team seat" in stats["changes"][-1]


async def test_unlinked_holders_lose_every_managed_role(teams) -> None:
    member = _member(2, OPS, TEAM_A, BLUETEAM)

    stats = await _sync(_guild(member))

    assert _ids(member.remove_roles) == {OPS, TEAM_A, BLUETEAM}
    assert all("(not linked)" in change for change in stats["changes"])


async def test_relinking_to_another_team_moves_the_team_role(teams) -> None:
    await _link(1, [], team=teams[1])
    member = _member(1, TEAM_A, BLUETEAM)

    await _sync(_guild(member))

    assert _ids(member.add_roles) == {TEAM_B}
    assert _ids(member.remove_roles) == {TEAM_A}


async def test_a_seat_on_a_deactivated_team_grants_nothing(teams) -> None:
    await Team.objects.filter(pk=teams[0].pk).aupdate(is_active=False)
    await _link(1, [], team=teams[0])
    member = _member(1, TEAM_A, BLUETEAM)

    await _sync(_guild(member))

    assert _ids(member.remove_roles) == {TEAM_A, BLUETEAM}


async def test_bots_are_left_alone(teams) -> None:
    bot_member = _member(3, OPS, TEAM_A, bot=True)

    await _sync(_guild(bot_member))

    bot_member.remove_roles.assert_not_awaited()


async def test_dry_run_reports_without_changing_anything(teams) -> None:
    await _link(1, ["WCComps_Ops"])
    granted, unlinked = _member(1), _member(2, TEAM_A)

    stats = await _sync(_guild(granted, unlinked), dry_run=True)

    for member in (granted, unlinked):
        member.add_roles.assert_not_awaited()
        member.remove_roles.assert_not_awaited()
    assert (stats["roles_added"], stats["roles_removed"]) == (1, 1)
    assert all(change.startswith("[DRY RUN]") for change in stats["changes"])


async def test_a_role_above_the_bot_is_reported_once_and_never_touched(teams, settings: Any) -> None:
    settings.GROUP_ROLE_MAPPING = {"WCComps_Ops": OPS, "WCComps_Boss": BOSS}
    await _link(1, ["WCComps_Boss"])
    members = [_member(1), _member(2, BOSS), _member(3, BOSS)]

    stats = await _sync(_guild(*members))

    for member in members:
        assert BOSS not in _ids(member.add_roles) | _ids(member.remove_roles)
    assert stats["errors"] == 1
    assert sum("Bot can't assign" in change for change in stats["changes"]) == 1


async def test_a_link_ended_during_the_pass_is_not_regranted(teams) -> None:
    link = await _link(1, [], team=teams[0])
    stale = await role_sync.sync_to_async(role_sync.role_grants)()
    await DiscordLink.objects.filter(pk=link.pk).aupdate(is_active=False)
    member = _member(1)

    real = role_sync.role_grants
    with patch.object(role_sync, "role_grants", side_effect=lambda ids=None: stale if ids is None else real(ids)):
        await _sync(_guild(member))

    member.add_roles.assert_not_awaited()


async def test_a_link_made_during_the_pass_keeps_its_role(teams) -> None:
    stale = await role_sync.sync_to_async(role_sync.role_grants)()
    await _link(1, ["WCComps_Ops"])
    member = _member(1, OPS)

    real = role_sync.role_grants
    with patch.object(role_sync, "role_grants", side_effect=lambda ids=None: stale if ids is None else real(ids)):
        await _sync(_guild(member))

    member.remove_roles.assert_not_awaited()


async def test_a_failed_update_is_counted_and_the_pass_goes_on(teams) -> None:
    failing, fine = _member(1, OPS), _member(2, OPS)
    failing.remove_roles.side_effect = discord.HTTPException(MagicMock(status=500), "boom")

    stats = await _sync(_guild(failing, fine))

    fine.remove_roles.assert_awaited_once()
    assert (stats["errors"], stats["roles_removed"]) == (1, 1)


async def test_without_manage_roles_nothing_is_touched_and_it_is_reported_once(teams) -> None:
    members = [_member(1, OPS), _member(2, TEAM_A)]
    guild = _guild(*members)
    guild.me.guild_permissions.manage_roles = False

    stats = await _sync(guild)

    for member in members:
        member.remove_roles.assert_not_awaited()
    assert stats["errors"] == 1
    assert stats["changes"] == ["Bot lacks the Manage Roles permission, so no roles were synced"]


async def test_syncing_one_member_gives_their_grant_and_takes_the_rest(teams) -> None:
    await _link(1, ["WCComps_Ops"], team=teams[1])
    member, bystander = _member(1, TEAM_A), _member(2, OPS)

    stats = dict(await role_sync.sync_member_roles(_guild(member, bystander), member))

    assert _ids(member.add_roles) == {OPS, TEAM_B, BLUETEAM}
    assert _ids(member.remove_roles) == {TEAM_A}
    bystander.remove_roles.assert_not_awaited()
    assert (stats["roles_added"], stats["roles_removed"]) == (3, 1)


async def test_syncing_one_member_after_unlinking_takes_every_synced_role(teams) -> None:
    link = await _link(1, ["WCComps_Ops"], team=teams[0])
    await DiscordLink.objects.filter(pk=link.pk).aupdate(is_active=False)
    member = _member(1, OPS, TEAM_A, BLUETEAM)

    await role_sync.sync_member_roles(_guild(member), member)

    assert _ids(member.remove_roles) == {OPS, TEAM_A, BLUETEAM}


async def test_refresh_loop_syncs_roles_after_refreshing_groups() -> None:
    calls: list[str] = []
    cog = authentik_groups.AuthentikGroupsCog.__new__(authentik_groups.AuthentikGroupsCog)
    cog.bot = MagicMock()

    async def refresh() -> None:
        calls.append("refresh")

    async def sync(bot: Any) -> None:
        calls.append("sync")

    with (
        patch.object(authentik_groups, "refresh_groups_now", refresh),
        patch.object(authentik_groups, "sync_roles_now", sync),
    ):
        await authentik_groups.AuthentikGroupsCog.refresh_task.coro(cog)

    assert calls == ["refresh", "sync"]


async def test_role_sync_failure_is_logged_not_raised(caplog: pytest.LogCaptureFixture) -> None:
    with (
        caplog.at_level(logging.WARNING, logger="bot.cogs.authentik_groups"),
        patch.object(authentik_groups, "competition_guild", return_value=MagicMock()),
        patch.object(authentik_groups, "sync_roles", AsyncMock(side_effect=RuntimeError("discord down"))),
    ):
        await authentik_groups.sync_roles_now(MagicMock())

    assert "Role sync failed: discord down" in caplog.text


async def test_a_long_sync_beats_the_calling_loop_per_member_it_changes(teams, monkeypatch) -> None:
    """A role sync queued as a task can outlast the queue's budget; each member updated is progress."""
    from bot import heartbeat

    beats: list[str] = []
    monkeypatch.setattr(heartbeat, "record", lambda name, bot: beats.append(name))
    members = [_member(member_id, OPS) for member_id in range(10, 15)]
    heartbeat.beat_for("queue", MagicMock())

    await _sync(_guild(_member(20), *members))

    assert beats == ["queue"] * 5
