"""Tests for unified dashboard functionality."""

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import discord
import pytest
from django.utils import timezone

from bot.unified_dashboard import UnifiedDashboard
from core.models import BotState
from team.models import Team
from ticketing.models import Ticket


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
class TestUnifiedDashboard:
    """Test unified dashboard functionality."""

    async def test_start_creates_task(self) -> None:
        """Test that start() creates background task."""
        bot = AsyncMock(spec=discord.Client)
        dashboard = UnifiedDashboard(bot)

        with patch("asyncio.create_task") as mock_create_task:
            mock_task = Mock()
            mock_task.cancel = Mock()
            mock_create_task.return_value = mock_task

            dashboard.start()

            assert dashboard.running is True
            assert dashboard.task is mock_task
            mock_create_task.assert_called_once()

            # Close the coroutine that was passed to the mocked create_task
            # to prevent "coroutine was never awaited" warning
            coro = mock_create_task.call_args[0][0]
            coro.close()

            dashboard.stop()

    async def test_stop_cancels_task(self) -> None:
        """Test that stop() cancels background task."""
        bot = AsyncMock(spec=discord.Client)
        dashboard = UnifiedDashboard(bot)

        mock_task = Mock()
        mock_task.cancel = Mock()
        dashboard.task = mock_task
        dashboard.running = True

        dashboard.stop()

        assert dashboard.running is False
        mock_task.cancel.assert_called_once()

    async def test_initialize_dashboard_creates_new_message(self) -> None:
        """Test _initialize_dashboard creates new message when none exists."""
        bot = AsyncMock(spec=discord.Client)
        dashboard = UnifiedDashboard(bot)

        mock_channel = MagicMock(spec=discord.TextChannel)
        mock_channel.id = 9999
        mock_message = AsyncMock()
        mock_message.id = 8888
        mock_channel.send = AsyncMock(return_value=mock_message)

        bot.get_channel.return_value = mock_channel

        with (
            patch.object(dashboard, "_update_dashboard", new_callable=AsyncMock) as mock_update,
            patch("bot.unified_dashboard.settings") as mock_settings,
        ):
            mock_settings.DISCORD_TICKET_QUEUE_CHANNEL_ID = 9999

            await dashboard._initialize_dashboard()

            assert dashboard.dashboard_message_id == 8888
            assert dashboard.dashboard_channel_id == 9999
            mock_channel.send.assert_called_once()
            mock_update.assert_called_once()

            # Verify state saved
            msg_state = await BotState.objects.aget(key="unified_dashboard_message_id")
            assert msg_state.value == "8888"

    async def test_initialize_dashboard_reconnects_existing(self) -> None:
        """Test _initialize_dashboard reconnects to existing message."""
        bot = AsyncMock(spec=discord.Client)
        dashboard = UnifiedDashboard(bot)

        await BotState.objects.acreate(key="unified_dashboard_message_id", value="7777")
        await BotState.objects.acreate(key="unified_dashboard_channel_id", value="6666")

        mock_channel = MagicMock(spec=discord.TextChannel)
        mock_channel.id = 6666
        mock_message = AsyncMock()
        mock_message.id = 7777
        mock_channel.fetch_message = AsyncMock(return_value=mock_message)

        bot.get_channel.return_value = mock_channel

        with patch.object(dashboard, "_update_dashboard", new_callable=AsyncMock) as mock_update:
            await dashboard._initialize_dashboard()

            assert dashboard.dashboard_message_id == 7777
            assert dashboard.dashboard_channel_id == 6666
            mock_channel.fetch_message.assert_called_once_with(7777)
            mock_update.assert_called_once()

    async def test_initialize_dashboard_fallback_on_reconnect_failure(self) -> None:
        """Test _initialize_dashboard creates new message if reconnect fails."""
        bot = AsyncMock(spec=discord.Client)
        dashboard = UnifiedDashboard(bot)

        await BotState.objects.acreate(key="unified_dashboard_message_id", value="5555")
        await BotState.objects.acreate(key="unified_dashboard_channel_id", value="4444")

        mock_channel = MagicMock(spec=discord.TextChannel)
        mock_channel.id = 9999
        mock_message = AsyncMock()
        mock_message.id = 3333
        mock_channel.send = AsyncMock(return_value=mock_message)

        mock_response = Mock()
        mock_response.status = 404
        mock_channel.fetch_message = AsyncMock(side_effect=discord.NotFound(mock_response, "message"))

        # First call returns None (old channel), second returns new channel
        bot.get_channel.side_effect = [mock_channel, mock_channel]

        with (
            patch.object(dashboard, "_update_dashboard", new_callable=AsyncMock),
            patch("bot.unified_dashboard.settings") as mock_settings,
        ):
            mock_settings.DISCORD_TICKET_QUEUE_CHANNEL_ID = 9999

            await dashboard._initialize_dashboard()

            assert dashboard.dashboard_message_id == 3333
            mock_channel.send.assert_called_once()

    async def test_triggers_between_passes_cost_one_update(self) -> None:
        dashboard = UnifiedDashboard(AsyncMock(spec=discord.Client))

        with patch.object(dashboard, "_update_dashboard", new_callable=AsyncMock) as mock_update:
            dashboard.trigger_update()
            dashboard.trigger_update()
            await dashboard._check_and_update()
            await dashboard._check_and_update()

        mock_update.assert_awaited_once()

    async def test_no_update_without_a_trigger(self) -> None:
        dashboard = UnifiedDashboard(AsyncMock(spec=discord.Client))

        with patch.object(dashboard, "_update_dashboard", new_callable=AsyncMock) as mock_update:
            await dashboard._check_and_update()

        mock_update.assert_not_awaited()

    async def test_failed_refresh_is_retried_next_pass(self) -> None:
        dashboard = UnifiedDashboard(AsyncMock(spec=discord.Client))
        dashboard.trigger_update()

        fails_once = AsyncMock(side_effect=[RuntimeError("db down"), None])
        with patch.object(dashboard, "_update_dashboard", fails_once) as update:
            with pytest.raises(RuntimeError):
                await dashboard._check_and_update()
            await dashboard._check_and_update()
            await dashboard._check_and_update()

        assert update.await_count == 2

    async def test_db_error_while_updating_propagates(self) -> None:
        """A pass with a pending refresh lets DB errors through, so the loop skips its heartbeat."""
        channel = MagicMock(spec=discord.TextChannel)
        channel.fetch_message = AsyncMock()
        bot = MagicMock(spec=discord.Client)
        bot.get_channel.return_value = channel
        dashboard = UnifiedDashboard(bot)
        dashboard.dashboard_message_id = 1
        dashboard.dashboard_channel_id = 2
        dashboard.trigger_update()

        with (
            patch("ticketing.models.Ticket.objects.filter", side_effect=RuntimeError("db down")),
            pytest.raises(RuntimeError, match="db down"),
        ):
            await dashboard._check_and_update()

    async def test_get_stale_indicator_no_assigned_at(self, box_reset_category) -> None:
        """Test _get_stale_indicator returns empty string for unassigned tickets."""
        bot = AsyncMock(spec=discord.Client)
        dashboard = UnifiedDashboard(bot)

        team = await Team.objects.acreate(team_number=1, team_name="Test Team")

        ticket = await Ticket.objects.acreate(
            ticket_number="T001-001",
            team=team,
            category=box_reset_category,
            title="Test",
            description="Test",
            status="open",
        )

        result = dashboard._get_stale_indicator(ticket)

        assert result == ""

    async def test_get_stale_indicator_under_30_minutes(self, box_reset_category) -> None:
        """Test _get_stale_indicator returns empty for tickets <30min."""
        bot = AsyncMock(spec=discord.Client)
        dashboard = UnifiedDashboard(bot)

        team = await Team.objects.acreate(team_number=2, team_name="Test Team")

        ticket = await Ticket.objects.acreate(
            ticket_number="T002-001",
            team=team,
            category=box_reset_category,
            title="Test",
            description="Test",
            status="claimed",
            assigned_at=timezone.now() - timedelta(minutes=15),
        )

        result = dashboard._get_stale_indicator(ticket)

        assert result == ""

    async def test_get_stale_indicator_over_30_minutes(self, box_reset_category) -> None:
        """Test _get_stale_indicator returns warning for tickets >30min."""
        bot = AsyncMock(spec=discord.Client)
        dashboard = UnifiedDashboard(bot)

        team = await Team.objects.acreate(team_number=3, team_name="Test Team")

        ticket = await Ticket.objects.acreate(
            ticket_number="T003-001",
            team=team,
            category=box_reset_category,
            title="Test",
            description="Test",
            status="claimed",
            assigned_at=timezone.now() - timedelta(minutes=45),
        )

        result = dashboard._get_stale_indicator(ticket)

        assert result == " ⚠️"

    async def test_get_stale_indicator_over_1_hour(self, box_reset_category) -> None:
        """Test _get_stale_indicator returns alert for tickets >1hr."""
        bot = AsyncMock(spec=discord.Client)
        dashboard = UnifiedDashboard(bot)

        team = await Team.objects.acreate(team_number=4, team_name="Test Team")

        ticket = await Ticket.objects.acreate(
            ticket_number="T004-001",
            team=team,
            category=box_reset_category,
            title="Test",
            description="Test",
            status="claimed",
            assigned_at=timezone.now() - timedelta(hours=1, minutes=30),
        )

        result = dashboard._get_stale_indicator(ticket)

        assert result == " 🚨"

    async def test_get_stale_indicator_over_2_hours(self, box_reset_category) -> None:
        """Test _get_stale_indicator returns critical for tickets >2hr."""
        bot = AsyncMock(spec=discord.Client)
        dashboard = UnifiedDashboard(bot)

        team = await Team.objects.acreate(team_number=5, team_name="Test Team")

        ticket = await Ticket.objects.acreate(
            ticket_number="T005-001",
            team=team,
            category=box_reset_category,
            title="Test",
            description="Test",
            status="claimed",
            assigned_at=timezone.now() - timedelta(hours=3),
        )

        result = dashboard._get_stale_indicator(ticket)

        assert result == " ⛔"

    async def test_get_time_ago_just_now(self) -> None:
        """Test _get_time_ago returns 'just now' for <1min."""
        bot = AsyncMock(spec=discord.Client)
        dashboard = UnifiedDashboard(bot)

        dt = timezone.now() - timedelta(seconds=30)
        result = dashboard._get_time_ago(dt)

        assert result == "just now"

    async def test_get_time_ago_minutes(self) -> None:
        """Test _get_time_ago returns minutes for <1hr."""
        bot = AsyncMock(spec=discord.Client)
        dashboard = UnifiedDashboard(bot)

        dt = timezone.now() - timedelta(minutes=15)
        result = dashboard._get_time_ago(dt)

        assert "15m ago" in result

    async def test_get_time_ago_hours(self) -> None:
        """Test _get_time_ago returns hours for <1day."""
        bot = AsyncMock(spec=discord.Client)
        dashboard = UnifiedDashboard(bot)

        dt = timezone.now() - timedelta(hours=5)
        result = dashboard._get_time_ago(dt)

        assert "5h ago" in result

    async def test_get_time_ago_days(self) -> None:
        """Test _get_time_ago returns days for >1day."""
        bot = AsyncMock(spec=discord.Client)
        dashboard = UnifiedDashboard(bot)

        dt = timezone.now() - timedelta(days=3)
        result = dashboard._get_time_ago(dt)

        assert "3d ago" in result

    async def test_get_time_ago_none(self) -> None:
        """Test _get_time_ago returns empty string for None."""
        bot = AsyncMock(spec=discord.Client)
        dashboard = UnifiedDashboard(bot)

        result = dashboard._get_time_ago(None)

        assert result == ""

    async def test_update_dashboard_no_tickets(self) -> None:
        """Test _update_dashboard with no active tickets."""
        bot = AsyncMock(spec=discord.Client)
        dashboard = UnifiedDashboard(bot)
        dashboard.dashboard_message_id = 1111
        dashboard.dashboard_channel_id = 2222

        mock_channel = MagicMock(spec=discord.TextChannel)
        mock_channel.id = 2222
        mock_channel.guild = Mock()
        mock_channel.guild.id = 3333
        mock_message = AsyncMock()
        mock_message.edit = AsyncMock()
        mock_channel.fetch_message = AsyncMock(return_value=mock_message)

        bot.get_channel.return_value = mock_channel

        await dashboard._update_dashboard()

        mock_message.edit.assert_called_once()
        call_kwargs = mock_message.edit.call_args[1]
        embed = call_kwargs["embed"]
        assert "No active tickets!" in embed.description

    async def test_update_dashboard_with_tickets(self, box_reset_category) -> None:
        """Test _update_dashboard displays tickets correctly."""
        bot = AsyncMock(spec=discord.Client)
        dashboard = UnifiedDashboard(bot)
        dashboard.dashboard_message_id = 1111
        dashboard.dashboard_channel_id = 2222

        team = await Team.objects.acreate(team_number=10, team_name="Test Team")

        await Ticket.objects.acreate(
            ticket_number="T010-001",
            team=team,
            category=box_reset_category,
            title="Test Ticket",
            description="Test description",
            status="open",
            discord_thread_id=9999,
        )

        mock_channel = MagicMock(spec=discord.TextChannel)
        mock_channel.id = 2222
        mock_channel.guild = Mock()
        mock_channel.guild.id = 3333
        mock_message = AsyncMock()
        mock_message.edit = AsyncMock()
        mock_channel.fetch_message = AsyncMock(return_value=mock_message)

        bot.get_channel.return_value = mock_channel

        await dashboard._update_dashboard()

        mock_message.edit.assert_called_once()
        call_kwargs = mock_message.edit.call_args[1]
        embed = call_kwargs["embed"]
        assert "1 active tickets" in embed.description

        # Verify ticket content appears in embed fields
        field_values = " ".join(f.value for f in embed.fields)
        assert "T010-001" in field_values
        assert "Test Team" in field_values

    async def test_update_dashboard_category_field_includes_playbook_steps(self, box_reset_category) -> None:
        """Category with playbook displays steps in the staff dashboard field."""
        box_reset_category.playbook = "1. First reset step\n2. Second reset step"
        await box_reset_category.asave(update_fields=["playbook"])

        bot = AsyncMock(spec=discord.Client)
        dashboard = UnifiedDashboard(bot)
        dashboard.dashboard_message_id = 1111
        dashboard.dashboard_channel_id = 2222

        team = await Team.objects.acreate(team_number=10, team_name="Test Team")
        await Ticket.objects.acreate(
            ticket_number="T010-002",
            team=team,
            category=box_reset_category,
            title="Reset Request",
            description="Reset please",
            status="open",
        )

        mock_channel = MagicMock(spec=discord.TextChannel)
        mock_channel.id = 2222
        mock_channel.guild = Mock()
        mock_channel.guild.id = 3333
        mock_message = AsyncMock()
        mock_message.edit = AsyncMock()
        mock_channel.fetch_message = AsyncMock(return_value=mock_message)
        bot.get_channel.return_value = mock_channel

        await dashboard._update_dashboard()

        call_kwargs = mock_message.edit.call_args[1]
        embed = call_kwargs["embed"]
        field_values = " ".join(f.value for f in embed.fields)
        assert "📋 **Playbook:**" in field_values
        assert "1. First reset step" in field_values
        assert "2. Second reset step" in field_values

    async def test_update_dashboard_category_field_omits_playbook_when_none(self, box_reset_category) -> None:
        """Category without playbook omits playbook in the staff dashboard field."""
        box_reset_category.playbook = ""
        await box_reset_category.asave(update_fields=["playbook"])

        bot = AsyncMock(spec=discord.Client)
        dashboard = UnifiedDashboard(bot)
        dashboard.dashboard_message_id = 1111
        dashboard.dashboard_channel_id = 2222

        team = await Team.objects.acreate(team_number=10, team_name="Test Team")
        await Ticket.objects.acreate(
            ticket_number="T010-003",
            team=team,
            category=box_reset_category,
            title="Reset Request",
            description="Reset please",
            status="open",
        )

        mock_channel = MagicMock(spec=discord.TextChannel)
        mock_channel.id = 2222
        mock_channel.guild = Mock()
        mock_channel.guild.id = 3333
        mock_message = AsyncMock()
        mock_message.edit = AsyncMock()
        mock_channel.fetch_message = AsyncMock(return_value=mock_message)
        bot.get_channel.return_value = mock_channel

        await dashboard._update_dashboard()

        call_kwargs = mock_message.edit.call_args[1]
        embed = call_kwargs["embed"]
        field_values = " ".join(f.value for f in embed.fields)
        assert "Playbook" not in field_values

    async def test_update_dashboard_sort_by_stale(self, box_reset_category) -> None:
        """Test _update_dashboard sorting by stale."""
        bot = AsyncMock(spec=discord.Client)
        dashboard = UnifiedDashboard(bot)
        dashboard.dashboard_message_id = 1111
        dashboard.dashboard_channel_id = 2222
        dashboard.sort_by = "stale"

        team = await Team.objects.acreate(team_number=11, team_name="Test Team")

        await Ticket.objects.acreate(
            ticket_number="T011-001",
            team=team,
            category=box_reset_category,
            title="Old Ticket",
            description="Old",
            status="claimed",
            assigned_at=timezone.now() - timedelta(hours=2),
        )

        await Ticket.objects.acreate(
            ticket_number="T011-002",
            team=team,
            category=box_reset_category,
            title="New Ticket",
            description="New",
            status="claimed",
            assigned_at=timezone.now() - timedelta(minutes=5),
        )

        mock_channel = MagicMock(spec=discord.TextChannel)
        mock_channel.id = 2222
        mock_channel.guild = Mock()
        mock_channel.guild.id = 3333
        mock_message = AsyncMock()
        mock_message.edit = AsyncMock()
        mock_channel.fetch_message = AsyncMock(return_value=mock_message)

        bot.get_channel.return_value = mock_channel

        await dashboard._update_dashboard()

        mock_message.edit.assert_called_once()
        call_kwargs = mock_message.edit.call_args[1]
        embed = call_kwargs["embed"]

        # Verify stale (2hr) ticket appears before fresh (5min) ticket
        field_values = " ".join(f.value for f in embed.fields)
        old_pos = field_values.index("T011-001")
        new_pos = field_values.index("T011-002")
        assert old_pos < new_pos, "Stale ticket should appear before fresh ticket"

    async def test_update_dashboard_sort_by_team(self, box_reset_category) -> None:
        """Test _update_dashboard sorting by team name."""
        bot = AsyncMock(spec=discord.Client)
        dashboard = UnifiedDashboard(bot)
        dashboard.dashboard_message_id = 1111
        dashboard.dashboard_channel_id = 2222
        dashboard.sort_by = "team"

        team_a = await Team.objects.acreate(team_number=20, team_name="Alpha Team")

        team_b = await Team.objects.acreate(team_number=21, team_name="Bravo Team")

        await Ticket.objects.acreate(
            ticket_number="T021-001",
            team=team_b,
            category=box_reset_category,
            title="Bravo Ticket",
            description="Bravo",
            status="open",
        )

        await Ticket.objects.acreate(
            ticket_number="T020-001",
            team=team_a,
            category=box_reset_category,
            title="Alpha Ticket",
            description="Alpha",
            status="open",
        )

        mock_channel = MagicMock(spec=discord.TextChannel)
        mock_channel.id = 2222
        mock_channel.guild = Mock()
        mock_channel.guild.id = 3333
        mock_message = AsyncMock()
        mock_message.edit = AsyncMock()
        mock_channel.fetch_message = AsyncMock(return_value=mock_message)

        bot.get_channel.return_value = mock_channel

        await dashboard._update_dashboard()

        mock_message.edit.assert_called_once()
        call_kwargs = mock_message.edit.call_args[1]
        embed = call_kwargs["embed"]

        # Verify Alpha Team appears before Bravo Team in embed
        field_values = " ".join(f.value for f in embed.fields)
        alpha_pos = field_values.index("Alpha Team")
        bravo_pos = field_values.index("Bravo Team")
        assert alpha_pos < bravo_pos, "Alpha should appear before Bravo when sorted by team"

    async def test_update_dashboard_filter_open(self, box_reset_category) -> None:
        """Test _update_dashboard filtering to only open tickets."""
        bot = AsyncMock(spec=discord.Client)
        dashboard = UnifiedDashboard(bot)
        dashboard.dashboard_message_id = 1111
        dashboard.dashboard_channel_id = 2222
        dashboard.filter_status = "open"

        team = await Team.objects.acreate(team_number=30, team_name="Test Team")

        await Ticket.objects.acreate(
            ticket_number="T030-001",
            team=team,
            category=box_reset_category,
            title="Open Ticket",
            description="Open",
            status="open",
        )

        await Ticket.objects.acreate(
            ticket_number="T030-002",
            team=team,
            category=box_reset_category,
            title="Claimed Ticket",
            description="Claimed",
            status="claimed",
        )

        mock_channel = MagicMock(spec=discord.TextChannel)
        mock_channel.id = 2222
        mock_channel.guild = Mock()
        mock_channel.guild.id = 3333
        mock_message = AsyncMock()
        mock_message.edit = AsyncMock()
        mock_channel.fetch_message = AsyncMock(return_value=mock_message)

        bot.get_channel.return_value = mock_channel

        await dashboard._update_dashboard()

        mock_message.edit.assert_called_once()
        call_kwargs = mock_message.edit.call_args[1]
        embed = call_kwargs["embed"]
        assert "Filter: Open" in embed.description

    async def test_update_dashboard_filter_claimed(self, box_reset_category) -> None:
        """Test _update_dashboard filtering to only claimed tickets."""
        bot = AsyncMock(spec=discord.Client)
        dashboard = UnifiedDashboard(bot)
        dashboard.dashboard_message_id = 1111
        dashboard.dashboard_channel_id = 2222
        dashboard.filter_status = "claimed"

        team = await Team.objects.acreate(team_number=31, team_name="Test Team")

        await Ticket.objects.acreate(
            ticket_number="T031-001",
            team=team,
            category=box_reset_category,
            title="Open Ticket",
            description="Open",
            status="open",
        )

        await Ticket.objects.acreate(
            ticket_number="T031-002",
            team=team,
            category=box_reset_category,
            title="Claimed Ticket",
            description="Claimed",
            status="claimed",
        )

        mock_channel = MagicMock(spec=discord.TextChannel)
        mock_channel.id = 2222
        mock_channel.guild = Mock()
        mock_channel.guild.id = 3333
        mock_message = AsyncMock()
        mock_message.edit = AsyncMock()
        mock_channel.fetch_message = AsyncMock(return_value=mock_message)

        bot.get_channel.return_value = mock_channel

        await dashboard._update_dashboard()

        mock_message.edit.assert_called_once()
        call_kwargs = mock_message.edit.call_args[1]
        embed = call_kwargs["embed"]
        assert "Filter: Claimed" in embed.description

    async def test_update_dashboard_handles_not_found(self) -> None:
        """Test _update_dashboard handles message not found."""
        bot = AsyncMock(spec=discord.Client)
        dashboard = UnifiedDashboard(bot)
        dashboard.dashboard_message_id = 1111
        dashboard.dashboard_channel_id = 2222

        mock_channel = MagicMock(spec=discord.TextChannel)
        mock_response = Mock()
        mock_response.status = 404
        mock_channel.fetch_message = AsyncMock(side_effect=discord.NotFound(mock_response, "message"))

        bot.get_channel.return_value = mock_channel

        await dashboard._update_dashboard()

        assert dashboard.dashboard_message_id is None
        assert dashboard.dashboard_channel_id is None
