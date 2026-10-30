"""Tests for ticket creation and resolution workflows."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from bot.discord_queue import DiscordQueueProcessor
from bot.tests.discord_fakes import BOT_USER, recording_thread, ticket_message
from core.models import DiscordTask
from team.models import Team
from ticketing.models import Ticket


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
class TestTicketCreationWorkflow:
    """Test ticket creation via web UI and Discord queue."""

    async def test_ticket_creation_end_to_end(self, db: Any, box_reset_category: Any) -> None:
        """Test ticket creation from web UI through Discord thread creation."""
        # Setup team and ticket
        team = await Team.objects.acreate(
            team_number=35,
            team_name="Test Team",
            discord_category_id=3001,
        )

        ticket = await Ticket.objects.acreate(
            ticket_number="T035-001",
            team=team,
            category=box_reset_category,
            title="Box Reset",
            description="Reset my box",
            status="open",
        )

        # Create Discord task (simulating web UI)
        task = await DiscordTask.objects.acreate(
            task_type="ticket_created_web",
            payload={
                "ticket_id": ticket.id,
                "ticket_number": ticket.ticket_number,
                "team_number": team.team_number,
                "category": "box-reset",
                "title": "Box Reset",
                "created_by": "test_user",
            },
            status="pending",
        )

        # Verify task created correctly
        assert task.task_type == "ticket_created_web"
        assert task.payload["ticket_id"] == ticket.id
        assert task.status == "pending"

        # Setup Discord mocks for queue processor
        bot = AsyncMock(spec=discord.Client)
        category = MagicMock(spec=discord.CategoryChannel)
        category.id = 3001
        category.name = "Test Team"

        text_channel = MagicMock(spec=discord.TextChannel)
        text_channel.name = "general-chat"
        text_channel.create_thread = AsyncMock()

        thread = MagicMock(spec=discord.Thread)
        thread.id = 9001
        thread.add_user = AsyncMock()
        thread.send = AsyncMock(return_value=MagicMock(id=900101, pin=AsyncMock()))
        text_channel.create_thread.return_value = thread

        category.channels = [text_channel]

        # A mock competition guild so the shared utility can find the channel
        guild = MagicMock(spec=discord.Guild)
        guild.get_channel.return_value = category
        guild.get_member.return_value = None  # No members to add

        # Process task (simulating queue processor)
        with (
            patch("bot.ticket_dashboard.trigger_dashboard"),
            patch("bot.discord_queue.competition_guild", return_value=guild),
        ):
            processor = DiscordQueueProcessor(bot)
            await processor._handle_ticket_created_web(task.typed_payload())

        # Verify thread created with correct metadata
        text_channel.create_thread.assert_called_once()
        call_args = text_channel.create_thread.call_args
        assert "T035-001" in call_args.kwargs["name"]
        assert "Team 35" in call_args.kwargs["name"]

        # Verify ticket updated with Discord IDs
        await ticket.arefresh_from_db()
        assert ticket.discord_thread_id == 9001

        # The real embed renders on the event loop (no sync ORM call escapes sync_to_async).
        sent = thread.send.await_args
        assert "T035-001" in sent.kwargs["embed"].title

    async def test_queue_processor_idempotent_thread_creation(self, db: Any, box_reset_category: Any) -> None:
        """Run again once the thread has its pinned ticket message, the task posts nothing and adds no one."""
        team = await Team.objects.acreate(
            team_number=37,
            team_name="Test Team 3",
            discord_category_id=3003,
        )

        ticket = await Ticket.objects.acreate(
            ticket_number="T037-001",
            team=team,
            category=box_reset_category,
            title="Box Reset",
            description="Help",
            status="open",
            discord_thread_id=9999,
        )

        task = await DiscordTask.objects.acreate(
            task_type="ticket_created_web",
            payload={
                "ticket_id": ticket.id,
                "ticket_number": ticket.ticket_number,
                "team_number": team.team_number,
                "category": "Box Reset",
                "title": "Box Reset",
                "created_by": "test_user",
            },
            status="pending",
        )

        thread = recording_thread(9999, pinned_text=ticket_message(ticket.ticket_number))
        bot = MagicMock(spec=discord.Client)
        bot.user = BOT_USER
        bot.get_channel.return_value = thread
        guild = MagicMock(spec=discord.Guild)

        with (
            patch("bot.discord_queue.trigger_dashboard") as mock_dashboard,
            patch("bot.discord_queue.competition_guild", return_value=guild),
        ):
            processor = DiscordQueueProcessor(bot)
            await processor._handle_ticket_created_web(task.typed_payload())

            mock_dashboard.assert_called_once()
        thread.send.assert_not_called()
        thread.add_user.assert_not_called()
        guild.get_channel.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
class TestTicketResolutionWorkflow:
    """Test ticket resolution via admin commands."""

    async def test_ticket_resolution_updates_status(
        self, mock_interaction: Any, mock_admin_user: Any, mock_bot: Any, scoring_check_category: Any
    ) -> None:
        """Test that resolving ticket updates status and timestamps."""
        from bot.cogs.admin_tickets import AdminTicketsCog

        mock_interaction.user.id = mock_admin_user._discord_id

        team = await Team.objects.acreate(
            team_number=38,
            team_name="Test Team Resolve",
        )

        ticket = await Ticket.objects.acreate(
            ticket_number="T038-001",
            team=team,
            category=scoring_check_category,
            title="Service Check",
            description="Check my service",
            status="claimed",
        )

        with patch("bot.cogs.admin_tickets.log_to_ops_channel", new_callable=AsyncMock):
            cog = AdminTicketsCog(mock_bot)
            await cog.admin_ticket_resolve.callback(
                cog,
                mock_interaction,
                ticket_number=ticket.ticket_number,
                notes="Fixed the issue",
                points=5,
            )

        # Refresh ticket with resolved_by relation (resolved_by is now User, not DiscordLink)
        ticket = await Ticket.objects.select_related("resolved_by").aget(pk=ticket.pk)

        assert ticket.status == "resolved"
        assert ticket.resolved_at is not None
        assert ticket.resolved_by is not None
        assert ticket.resolved_by.pk == mock_admin_user.pk
        assert ticket.resolution_notes == "Fixed the issue"
        assert ticket.points_charged == 5  # explicit points override the category default

    async def test_ticket_resolution_creates_history_log(
        self, mock_interaction: Any, mock_admin_user: Any, mock_bot: Any, other_category: Any
    ) -> None:
        """Test that resolving ticket creates history entry."""
        from bot.cogs.admin_tickets import AdminTicketsCog
        from ticketing.models import TicketHistory

        mock_interaction.user.id = mock_admin_user._discord_id

        team = await Team.objects.acreate(
            team_number=39,
            team_name="Test Team Audit",
        )

        ticket = await Ticket.objects.acreate(
            ticket_number="T039-001",
            team=team,
            category=other_category,
            title="General Issue",
            description="Problem",
            status="open",
        )

        with patch("bot.cogs.admin_tickets.log_to_ops_channel", new_callable=AsyncMock):
            cog = AdminTicketsCog(mock_bot)
            await cog.admin_ticket_resolve.callback(
                cog,
                mock_interaction,
                ticket_number=ticket.ticket_number,
                notes="All fixed",
                points=10,
            )

        history_entries = [h async for h in TicketHistory.objects.filter(ticket=ticket, action="resolved")]
        assert len(history_entries) >= 1

        history = history_entries[-1]
        assert history.action == "resolved"
        assert history.details["notes"] == "All fixed"
        assert history.details["point_penalty"] == 10

    async def test_ticket_resolution_queues_one_thread_update(
        self, mock_interaction: Any, mock_admin_user: Any, mock_bot: Any, box_reset_category: Any
    ) -> None:
        """The lifecycle queues the thread update (which refreshes the dashboard); the command adds none."""
        from bot.cogs.admin_tickets import AdminTicketsCog

        mock_interaction.user.id = mock_admin_user._discord_id

        team = await Team.objects.acreate(
            team_number=40,
            team_name="Test Team Dashboard",
        )

        ticket = await Ticket.objects.acreate(
            ticket_number="T040-001",
            team=team,
            category=box_reset_category,
            title="Reset",
            description="Reset",
            status="open",
        )

        with patch("bot.cogs.admin_tickets.log_to_ops_channel", new_callable=AsyncMock):
            cog = AdminTicketsCog(mock_bot)
            await cog.admin_ticket_resolve.callback(
                cog,
                mock_interaction,
                ticket_number=ticket.ticket_number,
                notes="Done",
                points=0,
            )

        tasks = [t async for t in DiscordTask.objects.filter(ticket=ticket)]
        assert [(t.task_type, t.payload["action"]) for t in tasks] == [("post_ticket_update", "resolved")]

    async def test_cannot_resolve_already_resolved_ticket(
        self, mock_interaction: Any, mock_admin_user: Any, mock_bot: Any, box_reset_category: Any
    ) -> None:
        """Test that already resolved tickets cannot be re-resolved."""
        from django.utils import timezone

        from bot.cogs.admin_tickets import AdminTicketsCog

        mock_interaction.user.id = mock_admin_user._discord_id

        team = await Team.objects.acreate(
            team_number=41,
            team_name="Test Team Double Resolve",
        )

        ticket = await Ticket.objects.acreate(
            ticket_number="T041-001",
            team=team,
            category=box_reset_category,
            title="Reset",
            description="Reset",
            status="resolved",
            resolved_at=timezone.now(),
        )

        cog = AdminTicketsCog(mock_bot)
        await cog.admin_ticket_resolve.callback(
            cog,
            mock_interaction,
            ticket_number=ticket.ticket_number,
            notes="Try again",
            points=5,
        )

        mock_interaction.response.send_message.assert_called_once()
        call_args = mock_interaction.response.send_message.call_args
        assert "cannot resolve ticket with status: resolved" in call_args.args[0].lower()
