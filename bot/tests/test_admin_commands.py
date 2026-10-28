"""Tests for admin slash commands."""

from contextlib import contextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from django.contrib.auth.models import User

from bot.cogs.admin_competition import AdminCompetitionCog
from bot.cogs.admin_teams import AdminTeamsCog
from bot.cogs.admin_tickets import AdminTicketsCog
from core.authentik_utils import CredentialReset
from core.models import AuditLog
from team.models import DiscordLink, Team
from ticketing.models import Ticket, TicketAttachment, TicketComment, TicketHistory


@contextmanager
def patch_globals(callback: Any, overrides: dict[str, Any]):
    """Patch a function's __globals__ directly.

    Under pytest-xdist, unittest.mock.patch on bot.cogs.* modules can target a
    different module object than the one the callback resolves names through.
    Patching __globals__ directly is the only reliable approach.
    """
    g = callback.__globals__
    orig = {k: g[k] for k in overrides if k in g}
    g.update(overrides)
    try:
        yield
    finally:
        g.update(orig)


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
class TestAdminCommands:
    """Test admin commands."""

    async def test_admin_teams_command(self, mock_interaction: Any, mock_admin_user: Any, mock_bot: Any) -> None:
        mock_interaction.user.id = mock_admin_user._discord_id

        await Team.objects.acreate(team_number=10, team_name="Team Alpha")
        await Team.objects.acreate(team_number=11, team_name="Team Beta")

        cog = AdminTeamsCog(mock_bot)
        await cog.admin_teams.callback(cog, mock_interaction)

        mock_interaction.response.send_message.assert_called_once()
        call_args = mock_interaction.response.send_message.call_args

        assert "embed" in call_args.kwargs
        embed = call_args.kwargs["embed"]
        assert embed.title == "Team Status (2 teams)"

    async def test_admin_team_info_command(self, mock_interaction: Any, mock_admin_user: Any, mock_bot: Any) -> None:
        mock_interaction.user.id = mock_admin_user._discord_id

        await Team.objects.acreate(team_number=12, team_name="Test Team")

        cog = AdminTeamsCog(mock_bot)
        await cog.admin_team_info.callback(cog, mock_interaction, team_number=12)

        mock_interaction.response.send_message.assert_called_once()
        call_args = mock_interaction.response.send_message.call_args
        assert "embed" in call_args.kwargs

    async def test_admin_team_info_not_found(self, mock_interaction: Any, mock_admin_user: Any, mock_bot: Any) -> None:
        mock_interaction.user.id = mock_admin_user._discord_id

        cog = AdminTeamsCog(mock_bot)
        await cog.admin_team_info.callback(cog, mock_interaction, team_number=42)

        mock_interaction.response.send_message.assert_called_once()
        call_args = mock_interaction.response.send_message.call_args
        assert "not found" in call_args.args[0].lower()

    async def test_reset_blueteam_passwords(
        self,
        mock_interaction: Any,
        mock_admin_user: Any,
        mock_bot: Any,
    ) -> None:
        """Test /admin reset-blueteam-passwords - verifies password reset flow."""
        mock_interaction.user.id = mock_admin_user._discord_id

        callback = AdminCompetitionCog.admin_reset_blueteam_passwords.callback
        with (
            patch("core.authentik_utils.AuthentikManager"),
            patch(
                "core.authentik_utils._reset_credentials",
                side_effect=lambda _m, n: CredentialReset(
                    password=f"pw-{n}", sessions_error="HTTP 403" if n == 2 else None
                ),
            ) as reset,
            patch_globals(callback, {"settings": MagicMock(AUTHENTIK_TOKEN="t")}),
        ):
            cog = AdminCompetitionCog(mock_bot)
            await cog.admin_reset_blueteam_passwords.callback(cog, mock_interaction, team_numbers="1-3")

        assert [c.args[1] for c in reset.call_args_list] == [1, 2, 3]
        mock_interaction.response.defer.assert_called_once_with(ephemeral=True)
        first, *rest = mock_interaction.followup.send.call_args_list
        csv_text = first.kwargs["file"].fp.getvalue().decode()
        assert csv_text.splitlines()[1:] == ["team01,pw-1", "team02,pw-2", "team03,pw-3"]
        assert any("Sessions: could not end sessions for team02: HTTP 403" in c.args[0] for c in rest)
        rows = [r.details["team_numbers"] async for r in AuditLog.objects.filter(action="blueteam_passwords_reset")]
        assert rows == [[1, 2, 3]]

    async def test_reset_blueteam_passwords_api_failure(
        self,
        mock_interaction: Any,
        mock_admin_user: Any,
        mock_bot: Any,
    ) -> None:
        """Test /admin reset-blueteam-passwords handles API failures."""
        mock_interaction.user.id = mock_admin_user._discord_id

        results = {
            1: CredentialReset(password="pw-1", sessions_found=0),
            2: CredentialReset(password=None, error="HTTP 500"),
            3: CredentialReset(password="pw-3", sessions_found=0),
        }
        callback = AdminCompetitionCog.admin_reset_blueteam_passwords.callback
        with (
            patch("core.authentik_utils.AuthentikManager"),
            patch("core.authentik_utils._reset_credentials", side_effect=lambda _m, n: results[n]),
            patch_globals(callback, {"settings": MagicMock(AUTHENTIK_TOKEN="t")}),
        ):
            cog = AdminCompetitionCog(mock_bot)
            await cog.admin_reset_blueteam_passwords.callback(cog, mock_interaction, team_numbers="1-3")

        first, *rest = mock_interaction.followup.send.call_args_list
        assert "Success: 2/3" in first.args[0]
        assert any("team02: HTTP 500" in c.args[0] for c in rest)
        csv_text = first.kwargs["file"].fp.getvalue().decode()
        assert "team02" not in csv_text, "a failed reset keeps its old password, so it must not be listed"

    async def test_reset_blueteam_passwords_long_details_never_lose_the_csv(
        self,
        mock_interaction: Any,
        mock_admin_user: Any,
        mock_bot: Any,
    ) -> None:
        """Details past Discord's 2000 characters must not take the CSV of new passwords down with them."""
        mock_interaction.user.id = mock_admin_user._discord_id
        callback = AdminCompetitionCog.admin_reset_blueteam_passwords.callback
        with (
            patch("core.authentik_utils.AuthentikManager"),
            patch(
                "core.authentik_utils._reset_credentials",
                side_effect=lambda _m, n: CredentialReset(
                    password=None if n % 2 else f"pw-{n}", error=f"failure {n} " + "x" * 80
                ),
            ),
            patch_globals(callback, {"settings": MagicMock(AUTHENTIK_TOKEN="t")}),
        ):
            cog = AdminCompetitionCog(mock_bot)
            await cog.admin_reset_blueteam_passwords.callback(cog, mock_interaction, team_numbers="1-50")

        sends = mock_interaction.followup.send.call_args_list
        assert "file" in sends[0].kwargs
        assert all(len(c.args[0]) <= 2000 for c in sends)
        assert sum(c.args[0].count("failure ") for c in sends) == 25

    async def test_admin_remove_team(
        self,
        mock_interaction: Any,
        mock_admin_user: Any,
        mock_bot: Any,
    ) -> None:
        """/admin remove-team unlinks the members, syncs their roles away, deletes the team's Discord objects."""
        mock_interaction.user.id = mock_admin_user._discord_id

        team_number = 13

        team = await Team.objects.acreate(
            team_number=team_number,
            team_name="Test Team to Remove",
            discord_role_id=1001,
            discord_category_id=2001,
        )

        # Create users and team members with active links
        member1_user = await User.objects.acreate(username="team_member1")
        member1_link = await DiscordLink.objects.acreate(
            discord_id=111111111,
            discord_username="member1",
            user=member1_user,
            team=team,
            is_active=True,
        )
        member2_user = await User.objects.acreate(username="team_member2")
        member2_link = await DiscordLink.objects.acreate(
            discord_id=222222222,
            discord_username="member2",
            user=member2_user,
            team=team,
            is_active=True,
        )

        # Create role mock
        team_role = MagicMock(spec=discord.Role)
        team_role.id = 1001
        team_role.name = f"Team {team_number:02d}"

        # Create Discord mocks for members with the team role
        member1_discord = MagicMock(spec=discord.Member)
        member1_discord.id = 111111111
        member1_discord.roles = [team_role]

        member2_discord = MagicMock(spec=discord.Member)
        member2_discord.id = 222222222
        member2_discord.roles = [team_role]

        category = MagicMock(spec=discord.CategoryChannel)
        category.id = 2001
        category.channels = []

        # Setup guild mock
        mock_interaction.guild.get_member = MagicMock(
            side_effect=lambda did: (
                member1_discord if did == 111111111 else (member2_discord if did == 222222222 else None)
            )
        )
        mock_interaction.guild.get_role = MagicMock(side_effect=lambda rid: team_role if rid == 1001 else None)
        mock_interaction.guild.get_channel = MagicMock(side_effect=lambda cid: category if cid == 2001 else None)

        category.delete = AsyncMock()
        team_role.delete = AsyncMock()

        synced: list[tuple[int, bool]] = []

        async def record_sync(guild: Any, member: Any) -> None:
            still_linked = await DiscordLink.objects.filter(discord_id=member.id, is_active=True).aexists()
            synced.append((member.id, still_linked))

        callback = AdminTeamsCog.admin_remove_team.callback
        with (
            patch("bot.role_sync.sync_member_roles", side_effect=record_sync),
            patch_globals(callback, {"log_to_ops_channel": AsyncMock()}),
        ):
            cog = AdminTeamsCog(mock_bot)
            await cog.admin_remove_team.callback(cog, mock_interaction, team_number=team_number)

            # Verify response was sent
            mock_interaction.response.defer.assert_called_once_with(ephemeral=True)
            mock_interaction.followup.send.assert_called_once()
            send_call_args = mock_interaction.followup.send.call_args
            assert "Removed" in send_call_args.args[0]

            # Each member's roles are synced once their link is already inactive, so they lose them
            assert sorted(synced) == [(111111111, False), (222222222, False)]
            await member1_link.arefresh_from_db()
            await member2_link.arefresh_from_db()
            assert member1_link.is_active is False
            assert member2_link.is_active is False
            assert member1_link.unlinked_at is not None
            assert member2_link.unlinked_at is not None

            # Verify category and role were deleted
            category.delete.assert_called_once()
            team_role.delete.assert_called_once()

            # Verify team Discord IDs were cleared
            await team.arefresh_from_db()
            assert team.discord_role_id is None
            assert team.discord_category_id is None

            # Verify audit log was created
            audit_logs = await AuditLog.objects.filter(
                action="team_removed",
                target_id=team_number,
            ).acount()
            assert audit_logs == 1

            audit_log = await AuditLog.objects.filter(
                action="team_removed",
                target_id=team_number,
            ).afirst()
            assert audit_log is not None
            assert audit_log.admin_user == str(mock_interaction.user)
            assert audit_log.target_entity == "team"
            assert audit_log.details["team_name"] == "Test Team to Remove"
            assert audit_log.details["unlinked_members"] == 2
            assert "role" in audit_log.details["removed_items"]

    async def test_admin_unlink_deactivates_link_and_removes_roles(
        self, mock_interaction: Any, mock_admin_user: Any, mock_bot: Any
    ) -> None:
        """admin_unlink deactivates the Discord link, then syncs the member's roles."""
        mock_interaction.user.id = mock_admin_user._discord_id
        mock_interaction.user.name = "admin_user"
        mock_interaction.user.mention = "<@211533935144992768>"

        # Create a team with discord role ID
        team = await Team.objects.acreate(
            team_number=14,
            team_name="Test Unlink Team",
            discord_role_id=1001,
        )

        # Create a team member with active link
        member_id = 999999999999999999
        team_user = await User.objects.acreate(username="team_user_01")

        await DiscordLink.objects.acreate(
            discord_id=member_id,
            discord_username="test_member",
            user=team_user,
            team=team,
            is_active=True,
        )

        # Mock Discord role
        team_role = MagicMock(spec=discord.Role)
        team_role.id = 1001
        team_role.name = team.team_name

        blueteam_role = MagicMock(spec=discord.Role)
        blueteam_role.id = 525444104763736075
        blueteam_role.name = "Blueteam"

        mock_interaction.guild.get_role.side_effect = lambda role_id: team_role if role_id == 1001 else blueteam_role

        # Mock member
        member_mock = AsyncMock(spec=discord.Member)
        member_mock.id = member_id
        member_mock.mention = f"<@{member_id}>"
        member_mock.remove_roles = AsyncMock()

        # Mock guild.get_member to return the member
        mock_interaction.guild.get_member.return_value = member_mock

        mock_log_ops = AsyncMock()
        still_linked_at_sync: list[bool] = []

        async def record_sync(guild: Any, member: Any) -> None:
            still_linked_at_sync.append(
                await DiscordLink.objects.filter(discord_id=member.id, is_active=True).aexists()
            )

        mock_sync = AsyncMock(side_effect=record_sync)

        callback = AdminTeamsCog.admin_unlink.callback
        with patch_globals(callback, {"sync_member_roles": mock_sync, "log_to_ops_channel": mock_log_ops}):
            cog = AdminTeamsCog(mock_bot)
            await cog.admin_unlink.callback(cog, mock_interaction, str(member_id))

            updated_link = await DiscordLink.objects.aget(discord_id=member_id)
            assert updated_link.is_active is False
            assert updated_link.unlinked_at is not None

            # The member's roles are synced after the link ends, so every synced role goes at once
            mock_sync.assert_awaited_once_with(mock_interaction.guild, member_mock)
            assert still_linked_at_sync == [False]

            # Verify audit log was created
            audit_logs = [log async for log in AuditLog.objects.filter(action="user_unlinked")]
            assert len(audit_logs) == 1
            audit_log = audit_logs[0]
            assert audit_log.admin_user == str(mock_interaction.user)
            assert audit_log.target_entity == "discord_link"
            assert audit_log.target_id == member_id
            assert audit_log.details["team_name"] == team.team_name
            assert audit_log.details["authentik_username"] == "team_user_01"

            # Verify user feedback message
            mock_interaction.response.defer.assert_called_once()
            mock_interaction.followup.send.assert_called_once()
            call_args = mock_interaction.followup.send.call_args
            assert "unlinked from" in call_args.args[0]

            mock_log_ops.assert_called_once()

    async def test_admin_unlink_user_not_linked(
        self, mock_interaction: Any, mock_admin_user: Any, mock_bot: Any
    ) -> None:
        """Test that admin_unlink handles user not linked to any team."""
        mock_interaction.user.id = mock_admin_user._discord_id

        # Create a member with no active link
        member_id = 888888888888888888

        member_mock = AsyncMock(spec=discord.Member)
        member_mock.id = member_id
        member_mock.mention = f"<@{member_id}>"

        # Mock guild.get_member to return the member
        mock_interaction.guild.get_member.return_value = member_mock

        cog = AdminTeamsCog(mock_bot)
        await cog.admin_unlink.callback(cog, mock_interaction, str(member_id))

        # Verify appropriate error message
        mock_interaction.response.defer.assert_called_once()
        mock_interaction.followup.send.assert_called_once()
        call_args = mock_interaction.followup.send.call_args
        assert "not linked to any team" in call_args.args[0] or "❌" in call_args.args[0]
        assert call_args.kwargs.get("ephemeral") is True

    async def test_admin_unlink_user_not_in_server(
        self, mock_interaction: Any, mock_admin_user: Any, mock_bot: Any
    ) -> None:
        """Test that admin_unlink handles user not in Discord server gracefully."""
        mock_interaction.user.id = mock_admin_user._discord_id
        mock_interaction.guild = None

        # Create a team
        team = await Team.objects.acreate(
            team_number=15,
            team_name="Test Team No Guild",
            discord_role_id=1001,
        )

        # Create a Discord link
        member_id = 777777777777777777
        old_user = await User.objects.acreate(username="old_user")
        await DiscordLink.objects.acreate(
            discord_id=member_id,
            discord_username="old_member",
            user=old_user,
            team=team,
            is_active=True,
        )

        member_mock = AsyncMock(spec=discord.Member)
        member_mock.id = member_id
        member_mock.mention = "<@test>"

        callback = AdminTeamsCog.admin_unlink.callback
        with patch_globals(
            callback,
            {
                "safe_remove_role": AsyncMock(),
                "log_to_ops_channel": AsyncMock(),
            },
        ):
            cog = AdminTeamsCog(mock_bot)
            await cog.admin_unlink.callback(cog, mock_interaction, str(member_id))

            # Since guild is None, command should return error early
            # Link should NOT be deactivated
            updated_link = await DiscordLink.objects.aget(discord_id=member_id)
            assert updated_link.is_active is True

            # Verify error message about guild requirement
            mock_interaction.response.defer.assert_called_once()
            mock_interaction.followup.send.assert_called_once()
            call_args = mock_interaction.followup.send.call_args
            assert "guild" in call_args.args[0].lower()
            assert call_args.kwargs.get("ephemeral") is True

    async def test_admin_clear_tickets(
        self,
        mock_interaction: Any,
        mock_admin_user: Any,
        mock_bot: Any,
        other_category: Any,
    ) -> None:
        """Test /tickets clear command deletes all tickets."""
        mock_interaction.user.id = mock_admin_user._discord_id

        # Create test teams and tickets
        team1 = await Team.objects.acreate(team_number=1, team_name="Team Alpha", ticket_counter=5)
        team2 = await Team.objects.acreate(team_number=2, team_name="Team Beta", ticket_counter=3)

        ticket1 = await Ticket.objects.acreate(
            ticket_number="T001-001",
            team=team1,
            category=other_category,
            title="Test Ticket 1",
            status="open",
        )

        await Ticket.objects.acreate(
            ticket_number="T002-001",
            team=team2,
            category=other_category,
            title="Test Ticket 2",
            status="claimed",
        )

        # Add related data
        await TicketComment.objects.acreate(
            ticket=ticket1,
            comment_text="Test comment",
        )

        await TicketAttachment.objects.acreate(
            ticket=ticket1,
            file_data=b"test data",
            filename="test.txt",
            mime_type="text/plain",
            uploaded_by="test_user",
        )

        await TicketHistory.objects.acreate(
            ticket=ticket1,
            action="created",
            details={"created_by": "test_user"},
        )

        # Verify initial state
        assert await Ticket.objects.acount() == 2
        assert await TicketComment.objects.acount() == 1
        assert await TicketAttachment.objects.acount() == 1
        assert await TicketHistory.objects.acount() == 1

        # Execute command with mocked view
        cog = AdminTicketsCog(mock_bot)

        # Store the view so we can manipulate it
        captured_view = None

        # Mock send_message to capture the view and set it to confirmed
        original_send = mock_interaction.response.send_message

        async def capture_and_confirm(*args, **kwargs):
            nonlocal captured_view
            if "view" in kwargs:
                captured_view = kwargs["view"]
                # Mock the wait to immediately return with confirmation
                captured_view.confirmed = True

                async def mock_wait():
                    return

                captured_view.wait = mock_wait
            return await original_send(*args, **kwargs)

        mock_interaction.response.send_message = capture_and_confirm

        callback = AdminTicketsCog.admin_ticket_clear.callback
        with patch_globals(callback, {"log_to_ops_channel": AsyncMock()}):
            await cog.admin_ticket_clear.callback(cog, mock_interaction)

        # Verify all tickets deleted
        assert await Ticket.objects.acount() == 0
        assert await TicketComment.objects.acount() == 0
        assert await TicketAttachment.objects.acount() == 0
        assert await TicketHistory.objects.acount() == 0

        # Verify counters reset
        team1 = await Team.objects.aget(team_number=1)
        team2 = await Team.objects.aget(team_number=2)
        assert team1.ticket_counter == 0
        assert team2.ticket_counter == 0

        # Verify audit log
        audit = await AuditLog.objects.filter(action="clear_tickets").afirst()
        assert audit is not None
        assert audit.details["tickets_deleted"] == 2

    async def test_admin_clear_tickets_no_tickets(
        self, mock_interaction: Any, mock_admin_user: Any, mock_bot: Any
    ) -> None:
        """Test /tickets clear with no tickets shows appropriate message."""
        mock_interaction.user.id = mock_admin_user._discord_id

        callback = AdminTicketsCog.admin_ticket_clear.callback
        with patch_globals(callback, {"log_to_ops_channel": AsyncMock()}):
            cog = AdminTicketsCog(mock_bot)
            await cog.admin_ticket_clear.callback(cog, mock_interaction)

        # Should send message about no tickets
        mock_interaction.response.send_message.assert_called_once()
        call_args = mock_interaction.response.send_message.call_args
        assert "No tickets or ticket counters to clear" in call_args.args[0]

    async def test_admin_clear_tickets_resets_leftover_counters_with_no_tickets(
        self, mock_interaction: Any, mock_admin_user: Any, mock_bot: Any
    ) -> None:
        """A wipe used to delete the tickets but keep the counters; /tickets clear must still reset them."""
        from team.models import Team

        mock_interaction.user.id = mock_admin_user._discord_id
        await Team.objects.acreate(team_number=9, team_name="Team 9", ticket_counter=85)
        original_send = mock_interaction.response.send_message

        async def confirm(*args, **kwargs):
            if "view" in kwargs:
                kwargs["view"].confirmed = True
                kwargs["view"].wait = AsyncMock()
            return await original_send(*args, **kwargs)

        mock_interaction.response.send_message = confirm
        callback = AdminTicketsCog.admin_ticket_clear.callback
        with patch_globals(callback, {"log_to_ops_channel": AsyncMock()}):
            cog = AdminTicketsCog(mock_bot)
            await cog.admin_ticket_clear.callback(cog, mock_interaction)

        assert (await Team.objects.aget(team_number=9)).ticket_counter == 0
        assert "Reset 1 team counters" in mock_interaction.edit_original_response.call_args.kwargs["content"]

    async def test_activate_teams(self, mock_interaction: Any, mock_admin_user: Any, mock_bot: Any) -> None:
        """Test /teams activate command."""
        mock_interaction.user.id = mock_admin_user._discord_id

        # Create teams - some active, some inactive
        await Team.objects.acreate(team_number=1, team_name="Team 01", is_active=False)
        await Team.objects.acreate(team_number=2, team_name="Team 02", is_active=False)
        await Team.objects.acreate(team_number=3, team_name="Team 03", is_active=True)

        mock_log_ops = AsyncMock()
        callback = AdminTeamsCog.admin_activate_teams.callback
        with patch_globals(callback, {"log_to_ops_channel": mock_log_ops}):
            cog = AdminTeamsCog(mock_bot)
            await cog.admin_activate_teams.callback(cog, mock_interaction, teams="1-3")

            mock_log_ops.assert_called_once()

        # Verify all teams are now active
        team1 = await Team.objects.aget(team_number=1)
        team2 = await Team.objects.aget(team_number=2)
        team3 = await Team.objects.aget(team_number=3)
        assert team1.is_active is True
        assert team2.is_active is True
        assert team3.is_active is True

        # Verify response was sent
        mock_interaction.response.defer.assert_called_once_with(ephemeral=True)
        mock_interaction.followup.send.assert_called_once()
        call_args = mock_interaction.followup.send.call_args
        assert "Activated" in call_args.args[0]

        # Verify audit log was created
        audit_logs = await AuditLog.objects.filter(action="teams_activated").acount()
        assert audit_logs == 1

    async def test_deactivate_teams(self, mock_interaction: Any, mock_admin_user: Any, mock_bot: Any) -> None:
        """Test /teams deactivate command."""
        mock_interaction.user.id = mock_admin_user._discord_id

        # Create teams - all active
        await Team.objects.acreate(team_number=4, team_name="Team 04", is_active=True)
        await Team.objects.acreate(team_number=5, team_name="Team 05", is_active=True)
        await Team.objects.acreate(team_number=6, team_name="Team 06", is_active=False)

        mock_log_ops = AsyncMock()
        callback = AdminTeamsCog.admin_deactivate_teams.callback
        with patch_globals(callback, {"log_to_ops_channel": mock_log_ops}):
            cog = AdminTeamsCog(mock_bot)
            await cog.admin_deactivate_teams.callback(cog, mock_interaction, teams="4,5,6")

            mock_log_ops.assert_called_once()

        # Verify all teams are now inactive
        team4 = await Team.objects.aget(team_number=4)
        team5 = await Team.objects.aget(team_number=5)
        team6 = await Team.objects.aget(team_number=6)
        assert team4.is_active is False
        assert team5.is_active is False
        assert team6.is_active is False

        # Verify response was sent
        mock_interaction.response.defer.assert_called_once_with(ephemeral=True)
        mock_interaction.followup.send.assert_called_once()
        call_args = mock_interaction.followup.send.call_args
        assert "Deactivated" in call_args.args[0]

        # Verify audit log was created
        audit_logs = await AuditLog.objects.filter(action="teams_deactivated").acount()
        assert audit_logs == 1

    async def test_recreate_teams(
        self,
        mock_interaction: Any,
        mock_admin_user: Any,
        mock_bot: Any,
    ) -> None:
        """Test /teams recreate command."""
        mock_interaction.user.id = mock_admin_user._discord_id

        # Create teams with existing Discord infrastructure
        await Team.objects.acreate(
            team_number=20,
            team_name="Team 20",
            discord_role_id=2001,
            discord_category_id=3001,
        )
        await Team.objects.acreate(
            team_number=21,
            team_name="Team 21",
            discord_role_id=2002,
            discord_category_id=3002,
        )

        # Mock Discord infrastructure
        role20 = MagicMock(spec=discord.Role)
        role20.id = 2001
        role20.delete = AsyncMock()

        role21 = MagicMock(spec=discord.Role)
        role21.id = 2002
        role21.delete = AsyncMock()

        category20 = MagicMock(spec=discord.CategoryChannel)
        category20.id = 3001
        category20.channels = []
        category20.delete = AsyncMock()

        category21 = MagicMock(spec=discord.CategoryChannel)
        category21.id = 3002
        category21.channels = []
        category21.delete = AsyncMock()

        mock_interaction.guild.get_role.side_effect = lambda rid: (
            role20 if rid == 2001 else (role21 if rid == 2002 else None)
        )
        mock_interaction.guild.get_channel.side_effect = lambda cid: (
            category20 if cid == 3001 else (category21 if cid == 3002 else None)
        )

        # Mock DiscordManager
        mock_manager = MagicMock()
        new_role = MagicMock(spec=discord.Role)
        new_category = MagicMock(spec=discord.CategoryChannel)
        new_category.channels = [MagicMock(), MagicMock()]  # Mock 2 channels
        mock_manager.setup_team_infrastructure = AsyncMock(return_value=(new_role, new_category))

        mock_log_ops = AsyncMock()
        callback = AdminTeamsCog.admin_recreate_teams.callback
        # DiscordManager is imported locally inside the method, so patch at the source module
        import bot.discord_manager

        orig_dm = bot.discord_manager.DiscordManager
        bot.discord_manager.DiscordManager = MagicMock(return_value=mock_manager)
        try:
            with patch_globals(callback, {"log_to_ops_channel": mock_log_ops}):
                cog = AdminTeamsCog(mock_bot)
                await cog.admin_recreate_teams.callback(cog, mock_interaction, teams="20,21")

                mock_log_ops.assert_called_once()
        finally:
            bot.discord_manager.DiscordManager = orig_dm

        # Verify old infrastructure was deleted
        role20.delete.assert_called_once()
        role21.delete.assert_called_once()
        category20.delete.assert_called_once()
        category21.delete.assert_called_once()

        # Verify new infrastructure was created
        assert mock_manager.setup_team_infrastructure.call_count == 2

        # Verify response was sent
        mock_interaction.response.defer.assert_called_once_with(ephemeral=True)
        mock_interaction.followup.send.assert_called_once()
        call_args = mock_interaction.followup.send.call_args
        assert "Success" in call_args.args[0] or "Recreated" in call_args.args[0]

        # Verify audit log was created
        audit_logs = await AuditLog.objects.filter(action="teams_recreated").acount()
        assert audit_logs == 1

        # Verify ops channel log
        mock_log_ops.assert_called_once()

    async def test_activate_invalid_range(self, mock_interaction: Any, mock_admin_user: Any, mock_bot: Any) -> None:
        """Test /teams activate with invalid team range."""
        mock_interaction.user.id = mock_admin_user._discord_id

        cog = AdminTeamsCog(mock_bot)
        await cog.admin_activate_teams.callback(cog, mock_interaction, teams="invalid")

        # Verify error was sent
        mock_interaction.response.defer.assert_called_once_with(ephemeral=True)
        mock_interaction.followup.send.assert_called_once()
        call_args = mock_interaction.followup.send.call_args
        assert "Error" in call_args.args[0] or "Invalid" in call_args.args[0]
