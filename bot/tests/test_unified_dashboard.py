"""Tests for unified dashboard functionality."""

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import aiohttp
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
        dashboard = UnifiedDashboard(MagicMock(spec=discord.Client))
        dashboard.message = AsyncMock()
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

        assert result == " (claimed >30m)"

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

        assert result == " (claimed >1h)"

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

        assert result == " (claimed >2h)"

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

        mock_channel = MagicMock(spec=discord.TextChannel)
        mock_channel.id = 2222
        mock_channel.guild = Mock()
        mock_channel.guild.id = 3333
        mock_message = AsyncMock()
        mock_message.edit = AsyncMock()
        dashboard.message = mock_message

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
        dashboard.message = mock_message

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
        assert "`OPEN`" in field_values

    async def test_update_dashboard_category_field_includes_playbook_steps(self, box_reset_category) -> None:
        """Category with playbook displays steps in the staff dashboard field."""
        box_reset_category.playbook = "1. First reset step\n2. Second reset step"
        await box_reset_category.asave(update_fields=["playbook"])

        bot = AsyncMock(spec=discord.Client)
        dashboard = UnifiedDashboard(bot)

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
        dashboard.message = mock_message
        bot.get_channel.return_value = mock_channel

        await dashboard._update_dashboard()

        call_kwargs = mock_message.edit.call_args[1]
        embed = call_kwargs["embed"]
        field_values = " ".join(f.value for f in embed.fields)
        assert "**Playbook:**" in field_values
        assert "1. First reset step" in field_values
        assert "2. Second reset step" in field_values

    async def test_update_dashboard_category_field_omits_playbook_when_none(self, box_reset_category) -> None:
        """Category without playbook omits playbook in the staff dashboard field."""
        box_reset_category.playbook = ""
        await box_reset_category.asave(update_fields=["playbook"])

        bot = AsyncMock(spec=discord.Client)
        dashboard = UnifiedDashboard(bot)

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
        dashboard.message = mock_message
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
        dashboard.message = mock_message

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
        dashboard.message = mock_message

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
        dashboard.message = mock_message

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
        dashboard.message = mock_message

        bot.get_channel.return_value = mock_channel

        await dashboard._update_dashboard()

        mock_message.edit.assert_called_once()
        call_kwargs = mock_message.edit.call_args[1]
        embed = call_kwargs["embed"]
        assert "Filter: Claimed" in embed.description

    async def test_update_dashboard_forgets_a_deleted_message(self) -> None:
        message = MagicMock(spec=discord.PartialMessage)
        message.edit = AsyncMock(side_effect=discord.NotFound(Mock(status=404), "Unknown Message"))
        dashboard = UnifiedDashboard(MagicMock(spec=discord.Client))
        dashboard.message = message

        await dashboard._update_dashboard()

        assert dashboard.message is None


class FakeQueueChannel:
    """The ticket queue channel as a partial messageable: messages it holds can be edited, others are NotFound."""

    def __init__(self, channel_id: int = 2222, *, send_error: Exception | None = None) -> None:
        self.id = channel_id
        self.messages: dict[int, MagicMock] = {}
        self.sent = 0
        self.send_error = send_error
        self.edit_error: Exception | None = None
        self.delete_error: Exception | None = None
        self._next_id = 4444

    def holding(self, message_id: int) -> FakeQueueChannel:
        self.messages[message_id] = self._message(message_id)
        return self

    def _message(self, message_id: int) -> MagicMock:
        message = MagicMock(spec=discord.PartialMessage)
        message.id = message_id
        message.channel = self

        async def edit(**_kwargs: object) -> None:
            if self.edit_error:
                raise self.edit_error
            if message_id not in self.messages:
                raise discord.NotFound(Mock(status=404), "Unknown Message")

        async def delete() -> None:
            if self.delete_error:
                raise self.delete_error
            self.messages.pop(message_id, None)

        message.edit = AsyncMock(side_effect=edit)
        message.delete = AsyncMock(side_effect=delete)
        return message

    def get_partial_message(self, message_id: int) -> MagicMock:
        return self.messages.get(message_id) or self._message(message_id)

    async def send(self, **_kwargs: object) -> MagicMock:
        if self.send_error:
            raise self.send_error
        self.sent += 1
        self._next_id += 1
        return self.holding(self._next_id).messages[self._next_id]


def _dashboard(*channels: FakeQueueChannel) -> UnifiedDashboard:
    bot = MagicMock(spec=discord.Client)
    by_id = {c.id: c for c in channels}
    bot.get_partial_messageable.side_effect = lambda channel_id, **_kwargs: by_id[channel_id]
    return UnifiedDashboard(bot)


async def _stored_message_id() -> str | None:
    return await BotState.objects.filter(key="unified_dashboard_message_id").values_list("value", flat=True).afirst()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
class TestDashboardMessage:
    """The dashboard keeps one message in the configured queue channel, whatever happens to it."""

    @pytest.fixture(autouse=True)
    def queue_channel(self, settings) -> None:
        settings.DISCORD_TICKET_QUEUE_CHANNEL_ID = 2222

    async def test_the_first_pass_posts_and_stores_a_message(self) -> None:
        channel = FakeQueueChannel()
        dashboard = _dashboard(channel)

        await dashboard._pass()

        assert channel.sent == 1
        assert await _stored_message_id() == str(dashboard.message.id)

    async def test_a_stored_message_still_there_is_reused_without_a_fetch(self) -> None:
        await BotState.objects.acreate(key="unified_dashboard_message_id", value="7777")
        channel = FakeQueueChannel().holding(7777)
        dashboard = _dashboard(channel)

        await dashboard._pass()

        assert channel.sent == 0
        assert dashboard.message.id == 7777
        channel.messages[7777].edit.assert_awaited_once()

    async def test_a_deleted_message_is_replaced_on_the_next_pass(self) -> None:
        channel = FakeQueueChannel().holding(7777)
        dashboard = _dashboard(channel)
        await BotState.objects.acreate(key="unified_dashboard_message_id", value="7777")
        await dashboard._pass()
        del channel.messages[7777]

        dashboard.trigger_update()
        await dashboard._pass()
        await dashboard._pass()

        assert channel.sent == 1
        assert await _stored_message_id() == str(dashboard.message.id) != "7777"

    @pytest.mark.parametrize(
        "error",
        [discord.HTTPException(Mock(status=503), "Service Unavailable"), TimeoutError()],
    )
    async def test_a_stored_message_that_cant_be_edited_is_reported_not_reposted(self, error: Exception) -> None:
        await BotState.objects.acreate(key="unified_dashboard_message_id", value="7777")
        channel = FakeQueueChannel().holding(7777)
        channel.edit_error = error
        dashboard = _dashboard(channel)

        with patch("bot.unified_dashboard.log_to_ops_channel", new_callable=AsyncMock) as ops:
            await dashboard._pass()

        assert channel.sent == 0
        ops.assert_awaited_once()
        assert type(error).__name__ in ops.await_args.args[1]

    async def test_a_5xx_with_a_different_body_each_time_is_reported_once(self) -> None:
        channel = FakeQueueChannel().holding(7777)
        dashboard = _dashboard(channel)
        await BotState.objects.acreate(key="unified_dashboard_message_id", value="7777")
        await dashboard._pass()

        with patch("bot.unified_dashboard.log_to_ops_channel", new_callable=AsyncMock) as ops:
            for ray in ("8a1b", "8a1c", "8a1d"):
                channel.edit_error = discord.HTTPException(Mock(status=502), f"<html>Bad gateway, Ray ID {ray}</html>")
                dashboard.trigger_update()
                await dashboard._pass()

        ops.assert_awaited_once()

    async def test_a_stored_message_whose_edit_failed_is_edited_next_pass(self) -> None:
        await BotState.objects.acreate(key="unified_dashboard_message_id", value="7777")
        channel = FakeQueueChannel().holding(7777)
        dashboard = _dashboard(channel)
        channel.edit_error = discord.HTTPException(Mock(status=503), "Service Unavailable")
        with patch("bot.unified_dashboard.log_to_ops_channel", new_callable=AsyncMock):
            await dashboard._pass()
        channel.edit_error = None
        message = channel.messages[7777]
        message.edit.reset_mock()

        await dashboard._pass()

        message.edit.assert_awaited_once()
        assert channel.sent == 0

    @pytest.mark.parametrize(
        "error",
        [
            discord.Forbidden(Mock(status=403), {"code": 50001, "message": "Missing Access"}),
            discord.HTTPException(Mock(status=400), {"code": 50005, "message": "Cannot edit another user's message"}),
        ],
    )
    async def test_a_stored_message_the_bot_cant_edit_is_replaced(self, error: Exception) -> None:
        await BotState.objects.acreate(key="unified_dashboard_message_id", value="7777")
        channel = FakeQueueChannel().holding(7777)
        channel.messages[7777].edit.side_effect = error
        dashboard = _dashboard(channel)

        await dashboard._pass()

        assert channel.sent == 1
        assert await _stored_message_id() == str(dashboard.message.id) != "7777"
        assert 7777 not in channel.messages

    async def test_an_embed_discord_rejects_keeps_the_message_and_is_reported_once(self) -> None:
        await BotState.objects.acreate(key="unified_dashboard_message_id", value="7777")
        channel = FakeQueueChannel().holding(7777)
        message = channel.messages[7777]
        message.edit.side_effect = discord.HTTPException(
            Mock(status=400), {"code": 50035, "message": "Invalid Form Body"}
        )
        dashboard = _dashboard(channel)

        with patch("bot.unified_dashboard.log_to_ops_channel", new_callable=AsyncMock) as ops:
            await dashboard._pass()
            await dashboard._pass()

        assert channel.sent == 0
        message.delete.assert_not_awaited()
        assert dashboard.message is message
        ops.assert_awaited_once()

    async def test_a_dashboard_that_cant_be_edited_or_deleted_is_reported_once_and_not_reposted(self) -> None:
        await BotState.objects.acreate(key="unified_dashboard_message_id", value="7777")
        channel = FakeQueueChannel().holding(7777)
        channel.messages[7777].edit.side_effect = discord.Forbidden(Mock(status=403), "Missing Access")
        channel.delete_error = discord.Forbidden(Mock(status=403), "Missing Permissions")
        dashboard = _dashboard(channel)

        with patch("bot.unified_dashboard.log_to_ops_channel", new_callable=AsyncMock) as ops:
            await dashboard._pass()
            await dashboard._pass()

        assert channel.sent == 0
        ops.assert_awaited_once()

    async def test_a_dashboard_that_stops_being_editable_is_replaced(self) -> None:
        channel = FakeQueueChannel().holding(7777)
        dashboard = _dashboard(channel)
        await BotState.objects.acreate(key="unified_dashboard_message_id", value="7777")
        await dashboard._pass()
        channel.messages[7777].edit.side_effect = discord.Forbidden(Mock(status=403), "Missing Access")

        with patch("bot.unified_dashboard.log_to_ops_channel", new_callable=AsyncMock) as ops:
            dashboard.trigger_update()
            await dashboard._pass()
            await dashboard._pass()

        assert channel.sent == 1
        assert await _stored_message_id() == str(dashboard.message.id) != "7777"
        ops.assert_not_awaited()

    async def test_a_new_dashboard_is_posted_with_its_tickets_in_one_send(self) -> None:
        channel = FakeQueueChannel()
        dashboard = _dashboard(channel)

        await dashboard._pass()

        assert channel.sent == 1
        dashboard.message.edit.assert_not_awaited()

    async def test_a_corrupt_stored_id_counts_as_no_dashboard(self) -> None:
        await BotState.objects.acreate(key="unified_dashboard_message_id", value="not a number")
        channel = FakeQueueChannel()
        dashboard = _dashboard(channel)

        await dashboard._pass()

        assert channel.sent == 1
        assert await _stored_message_id() == str(dashboard.message.id)

    async def test_alternating_failures_are_each_reported_once_until_a_pass_succeeds(self) -> None:
        channel = FakeQueueChannel().holding(7777)
        dashboard = _dashboard(channel)
        await BotState.objects.acreate(key="unified_dashboard_message_id", value="7777")
        await dashboard._pass()
        outage, refused = (
            discord.HTTPException(Mock(status=503), "Service Unavailable"),
            discord.Forbidden(Mock(status=403), "Missing Access"),
        )

        with patch("bot.unified_dashboard.log_to_ops_channel", new_callable=AsyncMock) as ops:
            for error in (outage, refused, outage, refused, None, outage):
                channel.edit_error = error
                dashboard.trigger_update()
                await dashboard._pass()

        assert ops.await_count == 3

    async def test_no_queue_channel_configured_is_fine(self, settings) -> None:
        settings.DISCORD_TICKET_QUEUE_CHANNEL_ID = 0
        dashboard = _dashboard()

        await dashboard._pass()

        assert dashboard.message is None

    async def test_a_dashboard_that_cannot_be_posted_is_reported_once_per_cause(self) -> None:
        """Restarting the bot can't fix a missing channel or permission, so the pass still counts."""
        channel = FakeQueueChannel(send_error=discord.Forbidden(Mock(status=403), "Missing Access"))
        dashboard = _dashboard(channel)

        with patch("bot.unified_dashboard.log_to_ops_channel", new_callable=AsyncMock) as ops:
            await dashboard._pass()
            await dashboard._pass()
            channel.send_error = discord.NotFound(Mock(status=404), "Unknown Channel")
            await dashboard._pass()

        reports = [call.args[1] for call in ops.await_args_list]
        assert len(reports) == 2
        assert reports[0].startswith("The ticket queue dashboard in <#2222> can't be posted or updated: Forbidden")
        assert "Missing Access" in reports[0]
        assert "Unknown Channel" in reports[1]

    @pytest.mark.parametrize(
        "error", [discord.Forbidden(Mock(status=403), "Missing Access"), TimeoutError(), aiohttp.ClientError()]
    )
    async def test_the_loop_beats_when_no_dashboard_can_be_posted(self, monkeypatch, error: Exception) -> None:
        import bot.unified_dashboard as module

        channel = FakeQueueChannel(send_error=error)
        dashboard = _dashboard(channel)
        dashboard.bot.wait_until_ready = AsyncMock()
        beats: list[str] = []

        async def one_pass(_seconds: float) -> None:
            dashboard.running = False

        monkeypatch.setattr(module, "recycle_db_connection", AsyncMock())
        monkeypatch.setattr(module, "record_heartbeat", lambda name, _bot: beats.append(name))
        monkeypatch.setattr(module, "log_to_ops_channel", AsyncMock())
        monkeypatch.setattr(module.asyncio, "sleep", one_pass)
        dashboard.running = True

        await dashboard._dashboard_loop()

        assert beats == ["dashboard"]
