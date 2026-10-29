"""Tests for ticketing cog commands and listeners."""

from datetime import timedelta
from unittest.mock import AsyncMock, Mock

import discord
import pytest
import pytest_asyncio
from django.conf import settings
from django.contrib.auth.models import User
from django.utils import timezone

from bot.cogs.ticketing import TicketingCog
from core.models import UserGroups
from team.models import DiscordLink, Team
from ticketing.models import Ticket, TicketCategory, TicketComment, TicketHistory


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
class TestTicketingCog:
    """Test TicketingCog class."""

    @pytest_asyncio.fixture
    async def bot(self) -> AsyncMock:
        """Create mock bot instance."""
        bot = AsyncMock(spec=discord.Client)
        bot.wait_until_ready = AsyncMock()
        bot.get_channel = Mock(return_value=None)
        return bot

    @pytest_asyncio.fixture
    async def cog(self, bot: AsyncMock) -> TicketingCog:
        """Create TicketingCog instance."""
        cog = TicketingCog(bot)
        # Stop the background task to avoid test interference
        cog.archive_threads_task.cancel()
        return cog

    @pytest_asyncio.fixture
    async def team(self) -> Team:
        """Create test team."""
        return await Team.objects.acreate(
            team_number=42,
            team_name="Test Team",
            authentik_group="WCComps_BlueTeam42",
            discord_category_id=1234567890,
        )

    @pytest_asyncio.fixture
    async def discord_link(self, team: Team) -> DiscordLink:
        """Create test Discord link."""
        user = await User.objects.acreate(username="testuser")
        await UserGroups.objects.acreate(user=user, authentik_id="testuser-uid", groups=[team.authentik_group])
        return await DiscordLink.objects.acreate(
            team=team,
            discord_id=999888777,
            discord_username="testuser#1234",
            user=user,
            is_active=True,
        )

    async def test_cog_initialization(self, bot: AsyncMock) -> None:
        """Test cog initializes and starts background task."""
        cog = TicketingCog(bot)
        assert cog.bot == bot
        # Task should be created
        assert hasattr(cog, "archive_threads_task")
        # Clean up
        cog.archive_threads_task.cancel()

    async def test_archive_threads_task_archives_expired_tickets(
        self, cog: TicketingCog, team: Team, box_reset_category: TicketCategory
    ) -> None:
        """Test background task archives tickets after grace period."""
        # Create ticket with thread scheduled for archiving in the past
        ticket = await Ticket.objects.acreate(
            ticket_number="T042-001",
            team=team,
            category=box_reset_category,
            title="Test Ticket",
            description="Test",
            status="resolved",
            discord_thread_id=1111222333,
            thread_archive_scheduled_at=timezone.now() - timedelta(seconds=120),
        )

        # Mock thread
        mock_thread = AsyncMock(spec=discord.Thread)
        mock_thread.guild.id = settings.COMPETITION_GUILD_ID
        mock_thread.edit = AsyncMock()
        cog.bot.get_channel = Mock(return_value=mock_thread)

        # Run the task once
        await cog.archive_threads_task()

        # Verify thread was archived
        mock_thread.edit.assert_called_once()
        call_kwargs = mock_thread.edit.call_args[1]
        assert call_kwargs["archived"] is True
        assert call_kwargs["locked"] is True

        # Verify database updated
        await ticket.arefresh_from_db()
        assert ticket.thread_archive_scheduled_at is None

        # Verify history entry created
        history_exists = await TicketHistory.objects.filter(ticket=ticket, action="thread_archived").aexists()
        assert history_exists

    async def test_archive_threads_task_skips_future_scheduled(
        self, cog: TicketingCog, team: Team, scoring_check_category: TicketCategory
    ) -> None:
        """Test background task skips tickets scheduled for future."""
        ticket = await Ticket.objects.acreate(
            ticket_number="T042-002",
            team=team,
            category=scoring_check_category,
            title="Future Archive",
            description="Test",
            status="resolved",
            discord_thread_id=2222333444,
            thread_archive_scheduled_at=timezone.now() + timedelta(minutes=5),
        )

        mock_thread = AsyncMock(spec=discord.Thread)
        mock_thread.guild.id = settings.COMPETITION_GUILD_ID
        cog.bot.get_channel = Mock(return_value=mock_thread)

        await cog.archive_threads_task()

        # Thread should not be archived
        mock_thread.edit.assert_not_called()

        # Database should be unchanged
        await ticket.arefresh_from_db()
        assert ticket.thread_archive_scheduled_at is not None

    async def test_archive_threads_task_handles_missing_thread(
        self, cog: TicketingCog, team: Team, scoring_check_category: TicketCategory
    ) -> None:
        """Test background task handles thread not found."""
        ticket = await Ticket.objects.acreate(
            ticket_number="T042-003",
            team=team,
            category=scoring_check_category,
            title="Missing Thread",
            description="Test",
            status="resolved",
            discord_thread_id=3333444555,
            thread_archive_scheduled_at=timezone.now() - timedelta(seconds=120),
        )

        # Thread not found
        cog.bot.get_channel = Mock(return_value=None)

        await cog.archive_threads_task()

        # Scheduled time should be cleared
        await ticket.arefresh_from_db()
        assert ticket.thread_archive_scheduled_at is None

    async def test_on_message_ignores_bot_messages(self, cog: TicketingCog) -> None:
        """Test on_message ignores messages from bots."""
        message = AsyncMock(spec=discord.Message)
        message.author = Mock()
        message.author.bot = True
        message.channel = Mock(spec=discord.Thread)

        initial_count = await TicketComment.objects.acount()
        await cog.on_message(message)

        # No comment should be created
        assert await TicketComment.objects.acount() == initial_count

    async def test_on_message_ignores_non_thread(self, cog: TicketingCog) -> None:
        """Test on_message ignores messages not in threads."""
        message = AsyncMock(spec=discord.Message)
        message.author = Mock()
        message.author.bot = False
        message.channel = Mock(spec=discord.TextChannel)  # Not a thread

        initial_count = await TicketComment.objects.acount()
        await cog.on_message(message)

        # No comment should be created
        assert await TicketComment.objects.acount() == initial_count

    async def test_on_message_edit_ignores_bot_messages(self, cog: TicketingCog) -> None:
        """Test on_message_edit ignores bot message edits."""
        before = Mock()
        after = AsyncMock(spec=discord.Message)
        after.author = Mock()
        after.author.bot = True

        await cog.on_message_edit(before, after)
        # Should complete without error

    async def test_on_message_edit_updates_comment(
        self, cog: TicketingCog, team: Team, scoring_check_category: TicketCategory
    ) -> None:
        """Test on_message_edit updates existing TicketComment."""
        ticket = await Ticket.objects.acreate(
            ticket_number="T042-007",
            team=team,
            category=scoring_check_category,
            title="Test Edit",
            description="Test",
            status="claimed",
            discord_thread_id=5555666777,
        )

        # Create existing comment
        comment = await TicketComment.objects.acreate(
            ticket=ticket,
            comment_text="Original text",
            discord_message_id=8888999000,
        )

        # Mock edited message
        before = Mock()
        after = AsyncMock(spec=discord.Message)
        after.author = Mock()
        after.author.bot = False
        after.content = "Edited text"
        after.id = comment.discord_message_id
        after.channel = Mock(spec=discord.Thread)
        after.channel.id = ticket.discord_thread_id

        await cog.on_message_edit(before, after)

        # Verify comment updated
        await comment.arefresh_from_db()
        assert comment.comment_text == "Edited text"

    async def test_on_message_delete_ignores_bot_messages(self, cog: TicketingCog) -> None:
        """Test on_message_delete ignores bot message deletes."""
        message = AsyncMock(spec=discord.Message)
        message.author = Mock()
        message.author.bot = True

        await cog.on_message_delete(message)
        # Should complete without error

    async def test_on_message_delete_marks_comment_deleted(
        self, cog: TicketingCog, team: Team, scoring_check_category: TicketCategory
    ) -> None:
        """Test on_message_delete soft-deletes TicketComment."""
        ticket = await Ticket.objects.acreate(
            ticket_number="T042-008",
            team=team,
            category=scoring_check_category,
            title="Test Delete",
            description="Test",
            status="claimed",
            discord_thread_id=6666777888,
        )

        # Create comment
        comment = await TicketComment.objects.acreate(
            ticket=ticket,
            comment_text="Will be deleted",
            discord_message_id=9999000111,
        )

        # Mock deleted message
        message = AsyncMock(spec=discord.Message)
        message.author = Mock()
        message.author.bot = False
        message.id = comment.discord_message_id
        message.channel = Mock(spec=discord.Thread)
        message.channel.id = ticket.discord_thread_id

        await cog.on_message_delete(message)

        # Verify comment marked as deleted
        await comment.arefresh_from_db()
        assert comment.comment_text == "[Message deleted]"

    async def test_archive_threads_task_multiple_tickets(
        self, cog: TicketingCog, team: Team, scoring_check_category: TicketCategory
    ) -> None:
        """Test archiving multiple tickets with mixed expiration times."""
        # Create 5 tickets - 3 expired, 2 not expired
        tickets = []
        for i in range(5):
            scheduled_at = timezone.now() - timedelta(seconds=120) if i < 3 else timezone.now() + timedelta(minutes=5)

            ticket = await Ticket.objects.acreate(
                ticket_number=f"T042-{200 + i}",
                team=team,
                category=scoring_check_category,
                title=f"Test {i}",
                description="Test",
                status="resolved",
                discord_thread_id=2000000 + i,
                thread_archive_scheduled_at=scheduled_at,
            )
            tickets.append(ticket)

        # Mock threads
        mock_threads = {}
        for ticket in tickets:
            mock_thread = AsyncMock(spec=discord.Thread)
            mock_thread.guild.id = settings.COMPETITION_GUILD_ID
            mock_thread.edit = AsyncMock()
            mock_threads[ticket.discord_thread_id] = mock_thread

        cog.bot.get_channel = lambda tid: mock_threads.get(tid)

        await cog.archive_threads_task()

        # Verify only expired tickets were archived (first 3)
        for i in range(3):
            mock_threads[tickets[i].discord_thread_id].edit.assert_called_once()

        # Verify unexpired tickets were not archived (last 2)
        for i in range(3, 5):
            mock_threads[tickets[i].discord_thread_id].edit.assert_not_called()

    async def test_on_message_creates_comment(
        self, cog: TicketingCog, team: Team, scoring_check_category: TicketCategory
    ) -> None:
        """Test on_message creates TicketComment for Discord messages."""
        ticket = await Ticket.objects.acreate(
            ticket_number="T042-100",
            team=team,
            category=scoring_check_category,
            title="Test Comment Sync",
            description="Test",
            status="open",
            discord_thread_id=7777888999,
        )

        # Create user and discord link for the message author
        author_user = await User.objects.acreate(username="comment_author")
        await DiscordLink.objects.acreate(
            discord_id=123456789,
            discord_username="testuser#1234",
            user=author_user,
            team=team,
            is_active=True,
        )

        # Mock Discord message
        message = AsyncMock(spec=discord.Message)
        message.author = Mock()
        message.author.bot = False
        message.author.id = 123456789
        message.author.__str__ = Mock(return_value="testuser#1234")
        message.id = 999888777666
        message.content = "This is a test message"
        message.channel = Mock(spec=discord.Thread)
        message.channel.id = ticket.discord_thread_id
        message.attachments = []

        initial_count = await TicketComment.objects.acount()
        await cog.on_message(message)

        # Verify comment created
        assert await TicketComment.objects.acount() == initial_count + 1

        # Verify comment has correct data (author is now a User, not DiscordLink)
        comment = await TicketComment.objects.select_related("author").filter(discord_message_id=message.id).afirst()
        assert comment is not None
        assert comment.ticket_id == ticket.id
        assert comment.author is not None
        assert comment.author.username == "comment_author"
        assert comment.comment_text == "This is a test message"
        assert comment.discord_message_id == 999888777666

    async def test_on_message_prevents_duplicate_comments(
        self, cog: TicketingCog, team: Team, scoring_check_category: TicketCategory
    ) -> None:
        """Test on_message doesn't create duplicate comments."""
        ticket = await Ticket.objects.acreate(
            ticket_number="T042-101",
            team=team,
            category=scoring_check_category,
            title="Test Duplicate",
            description="Test",
            status="open",
            discord_thread_id=8888999111,
        )

        # Create existing comment
        await TicketComment.objects.acreate(
            ticket=ticket,
            comment_text="Original",
            discord_message_id=111222333444,
        )

        # Mock Discord message with same ID
        message = AsyncMock(spec=discord.Message)
        message.author = Mock()
        message.author.bot = False
        message.author.id = 123456789
        message.author.__str__ = Mock(return_value="testuser#1234")
        message.id = 111222333444  # Same as existing
        message.content = "Duplicate attempt"
        message.channel = Mock(spec=discord.Thread)
        message.channel.id = ticket.discord_thread_id
        message.attachments = []

        initial_count = await TicketComment.objects.acount()
        await cog.on_message(message)

        # Should not create duplicate
        assert await TicketComment.objects.acount() == initial_count

    async def test_on_message_ignores_empty_content(
        self, cog: TicketingCog, team: Team, scoring_check_category: TicketCategory
    ) -> None:
        """Test on_message ignores messages with no text content."""
        ticket = await Ticket.objects.acreate(
            ticket_number="T042-102",
            team=team,
            category=scoring_check_category,
            title="Test Empty",
            description="Test",
            status="open",
            discord_thread_id=9999000222,
        )

        # Mock Discord message with empty content
        message = AsyncMock(spec=discord.Message)
        message.author = Mock()
        message.author.bot = False
        message.author.id = 123456789
        message.content = ""  # Empty content
        message.channel = Mock(spec=discord.Thread)
        message.channel.id = ticket.discord_thread_id
        message.attachments = []

        initial_count = await TicketComment.objects.acount()
        await cog.on_message(message)

        # Should not create comment for empty message
        assert await TicketComment.objects.acount() == initial_count

    async def test_on_message_for_nonexistent_ticket(self, cog: TicketingCog) -> None:
        """Test on_message handles messages in threads with no ticket."""
        # Mock Discord message in thread that doesn't have a ticket
        message = AsyncMock(spec=discord.Message)
        message.author = Mock()
        message.author.bot = False
        message.author.id = 123456789
        message.author.__str__ = Mock(return_value="testuser#1234")
        message.id = 555444333222
        message.content = "This thread has no ticket"
        message.channel = Mock(spec=discord.Thread)
        message.channel.id = 111111111111  # Non-existent thread
        message.attachments = []

        initial_count = await TicketComment.objects.acount()
        await cog.on_message(message)

        # Should not create comment for non-ticket thread
        assert await TicketComment.objects.acount() == initial_count


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
class TestTicketCommand:
    """Test the /ticket slash command."""

    @pytest_asyncio.fixture
    async def bot(self) -> AsyncMock:
        """Create mock bot instance."""
        bot = AsyncMock(spec=discord.Client)
        bot.wait_until_ready = AsyncMock()
        bot.get_channel = Mock(return_value=None)
        return bot

    @pytest_asyncio.fixture
    async def cog(self, bot: AsyncMock) -> TicketingCog:
        """Create TicketingCog instance."""
        cog = TicketingCog(bot)
        cog.archive_threads_task.cancel()
        return cog

    @pytest_asyncio.fixture
    async def team(self) -> Team:
        """Create test team with Discord infrastructure."""
        return await Team.objects.acreate(
            team_number=50,
            team_name="Test Team 50",
            authentik_group="WCComps_BlueTeam50",
            discord_category_id=9876543210,
        )

    @pytest_asyncio.fixture
    async def discord_link(self, team: Team) -> DiscordLink:
        """Create test Discord link for team member."""
        from django.contrib.auth.models import User

        user = await User.objects.acreate(username="teammember", email="teammember@test.local")
        await UserGroups.objects.acreate(user=user, authentik_id="teammember-uid", groups=[team.authentik_group])
        return await DiscordLink.objects.acreate(
            team=team,
            user=user,
            discord_id=555666777888,
            discord_username="teammember#9999",
            is_active=True,
        )

    @pytest_asyncio.fixture
    def interaction(self, discord_link: DiscordLink) -> AsyncMock:
        """Create mock interaction for team member."""
        interaction = AsyncMock(spec=discord.Interaction)
        interaction.user = Mock()
        interaction.user.id = discord_link.discord_id
        interaction.user.name = "teammember"
        interaction.user.__str__ = Mock(return_value="teammember#9999")
        interaction.guild = Mock(spec=discord.Guild)
        interaction.guild.id = 525435725123158026
        interaction.response = AsyncMock()
        interaction.followup = AsyncMock()
        return interaction

    async def test_create_ticket_invalid_category(
        self, cog: TicketingCog, team: Team, discord_link: DiscordLink, interaction: AsyncMock
    ) -> None:
        """Test /ticket with invalid category."""
        # Pass a non-existent category PK (string, since cog does int(category))
        await cog.create_ticket.callback(cog, interaction, category="9999", description="test")

        # Should send error message
        interaction.response.send_message.assert_called_once()
        args = interaction.response.send_message.call_args
        assert "invalid" in args[0][0].lower()

    async def test_create_ticket_box_reset_requires_hostname(
        self,
        cog: TicketingCog,
        team: Team,
        discord_link: DiscordLink,
        interaction: AsyncMock,
        box_reset_category: TicketCategory,
    ) -> None:
        """Test /ticket box-reset requires hostname parameter."""
        await cog.create_ticket.callback(
            cog, interaction, category="2", description="need reset", hostname=None, ip_address=None
        )

        # Should send error message about missing fields
        interaction.response.send_message.assert_called_once()
        args = interaction.response.send_message.call_args
        assert "hostname" in args[0][0].lower()

    async def test_create_ticket_scoring_check_requires_service(
        self,
        cog: TicketingCog,
        team: Team,
        discord_link: DiscordLink,
        interaction: AsyncMock,
        scoring_check_category: TicketCategory,
    ) -> None:
        """Test /ticket scoring-service-check requires service parameter."""
        await cog.create_ticket.callback(cog, interaction, category="3", description="check this", service=None)

        # Should send error message about missing service
        interaction.response.send_message.assert_called_once()
        args = interaction.response.send_message.call_args
        assert "service" in args[0][0].lower()

    async def test_create_ticket_with_required_fields_succeeds(
        self,
        cog: TicketingCog,
        team: Team,
        discord_link: DiscordLink,
        interaction: AsyncMock,
        box_reset_category: TicketCategory,
    ) -> None:
        """Test /ticket with all required fields creates ticket."""
        from unittest.mock import patch

        # Mock thread creation
        mock_thread = AsyncMock()
        mock_thread.id = 9999888877776666
        mock_thread.send = AsyncMock()
        mock_thread.add_user = AsyncMock()

        # Create proper mock for TextChannel with correct isinstance check
        mock_channel = Mock(spec=discord.TextChannel)
        mock_channel.name = "general-chat"
        mock_channel.create_thread = AsyncMock(return_value=mock_thread)

        mock_category = Mock(spec=discord.CategoryChannel)
        mock_category.id = team.discord_category_id
        mock_category.channels = [mock_channel]

        interaction.guild.get_channel = Mock(return_value=mock_category)

        with patch("bot.thread_creator.trigger_dashboard"):
            await cog.create_ticket.callback(
                cog,
                interaction,
                category="2",
                description="please reset my web server",
                hostname="webserver01",
                ip_address="10.0.0.5",
                service=None,
            )

        # Should send success message
        interaction.response.send_message.assert_called_once()
        args, kwargs = interaction.response.send_message.call_args
        embed = kwargs.get("embed") or args[0]
        assert "Ticket Created" in embed.title

        # Verify ticket was created with all fields
        ticket = await Ticket.objects.filter(team=team).order_by("-created_at").afirst()
        assert ticket is not None
        assert ticket.hostname == "webserver01"
        assert ticket.ip_address == "10.0.0.5"

        # The thread gets the ticket embed with its action buttons.
        assert ticket.discord_thread_id == mock_thread.id
        sent = mock_thread.send.await_args
        assert ticket.ticket_number in sent.kwargs["embed"].title
        assert sent.kwargs["view"] is not None

    async def test_create_ticket_auto_populates_ip_from_hostname(
        self,
        cog: TicketingCog,
        team: Team,
        discord_link: DiscordLink,
        interaction: AsyncMock,
        box_reset_category: TicketCategory,
    ) -> None:
        """Test /ticket auto-populates IP address from hostname."""
        from unittest.mock import MagicMock, patch

        # Mock infrastructure data
        mock_infrastructure = MagicMock()
        mock_box = MagicMock()
        mock_box.name = "webserver01"
        mock_box.ip = "10.0.0.99"
        mock_box.services = []
        mock_infrastructure.boxes = [mock_box]

        mock_client = MagicMock()
        mock_client.get_infrastructure.return_value = mock_infrastructure
        mock_client.get_service_choices.return_value = []

        # Mock thread creation
        mock_thread = AsyncMock()
        mock_thread.id = 9999888877776667
        mock_thread.send = AsyncMock()
        mock_thread.add_user = AsyncMock()

        mock_channel = Mock(spec=discord.TextChannel)
        mock_channel.name = "general-chat"
        mock_channel.create_thread = AsyncMock(return_value=mock_thread)

        mock_category = Mock(spec=discord.CategoryChannel)
        mock_category.id = team.discord_category_id
        mock_category.channels = [mock_channel]

        interaction.guild.get_channel = Mock(return_value=mock_category)

        with (
            patch("bot.thread_creator.trigger_dashboard"),
            patch("bot.cogs.ticketing.get_quotient_client", return_value=mock_client),
        ):
            await cog.create_ticket.callback(
                cog,
                interaction,
                category="2",
                description="please reset",
                hostname="webserver01",
                ip_address=None,  # Not provided - should be auto-populated
                service=None,
            )

        # Verify ticket was created with auto-populated IP
        ticket = await Ticket.objects.filter(team=team).order_by("-created_at").afirst()
        assert ticket is not None
        assert ticket.hostname == "webserver01"
        assert ticket.ip_address == "10.0.0.99"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
class TestRateLimiting:
    """Test rate limiting for ticket comments."""

    @pytest_asyncio.fixture
    async def bot(self) -> AsyncMock:
        """Create mock bot instance."""
        bot = AsyncMock(spec=discord.Client)
        return bot

    @pytest_asyncio.fixture
    async def cog(self, bot: AsyncMock) -> TicketingCog:
        """Create TicketingCog instance."""
        cog = TicketingCog(bot)
        cog.archive_threads_task.cancel()
        return cog

    @pytest_asyncio.fixture
    async def team(self) -> Team:
        """Create test team."""
        return await Team.objects.acreate(
            team_number=25,
            team_name="Rate Limit Team",
            authentik_group="WCComps_BlueTeam25",
        )

    @pytest_asyncio.fixture
    async def ticket(self, team: Team, scoring_check_category: TicketCategory) -> Ticket:
        """Create test ticket."""
        return await Ticket.objects.acreate(
            ticket_number="T025-500",
            team=team,
            category=scoring_check_category,
            title="Rate Limit Test",
            description="Test",
            status="open",
            discord_thread_id=1234567890123,
        )

    async def test_rate_limit_per_ticket_enforced(self, cog: TicketingCog, ticket: Ticket) -> None:
        """Test rate limit: 5 comments per minute per ticket."""
        from ticketing.models import CommentRateLimit

        # Create 5 rate limit entries for this ticket (at the limit)
        for i in range(5):
            await CommentRateLimit.objects.acreate(ticket=ticket, discord_id=111222333 + i)

        # Create message from a user
        message = AsyncMock(spec=discord.Message)
        message.author = Mock()
        message.author.bot = False
        message.author.id = 999888777  # Different user
        message.author.mention = "<@999888777>"
        message.channel = Mock(spec=discord.Thread)
        message.channel.id = ticket.discord_thread_id
        message.channel.send = AsyncMock()
        message.content = "This should be rate limited"
        message.attachments = []
        message.delete = AsyncMock()

        await cog.on_message(message)

        # Message should be deleted
        message.delete.assert_called_once()

        # Rate limit message should be sent
        message.channel.send.assert_called_once()
        args = message.channel.send.call_args
        assert "rate limit" in args[0][0].lower()

    async def test_rate_limit_per_user_enforced(self, cog: TicketingCog, ticket: Ticket, team: Team) -> None:
        """Test rate limit: 10 comments per minute per user."""
        from ticketing.models import CommentRateLimit

        # Create 10 rate limit entries for same user (user-level limit is 10/min)
        user_id = 555444333
        for _i in range(10):
            await CommentRateLimit.objects.acreate(ticket=ticket, discord_id=user_id)

        # Try to comment on the original ticket
        message = AsyncMock(spec=discord.Message)
        message.author = Mock()
        message.author.bot = False
        message.author.id = user_id
        message.id = 444555666777  # Add message ID
        message.author.mention = f"<@{user_id}>"
        message.channel = Mock(spec=discord.Thread)
        message.channel.id = ticket.discord_thread_id
        message.channel.send = AsyncMock()
        message.content = "User rate limited"
        message.attachments = []
        message.delete = AsyncMock()

        await cog.on_message(message)

        # Message should be deleted due to user-level rate limit
        message.delete.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
class TestAttachmentHandling:
    """Test file attachment handling."""

    @pytest_asyncio.fixture
    async def bot(self) -> AsyncMock:
        """Create mock bot instance."""
        return AsyncMock(spec=discord.Client)

    @pytest_asyncio.fixture
    async def cog(self, bot: AsyncMock) -> TicketingCog:
        """Create TicketingCog instance."""
        cog = TicketingCog(bot)
        cog.archive_threads_task.cancel()
        return cog

    @pytest_asyncio.fixture
    async def team(self) -> Team:
        """Create test team."""
        return await Team.objects.acreate(
            team_number=35,
            team_name="Attachment Team",
            authentik_group="WCComps_BlueTeam35",
        )

    @pytest_asyncio.fixture
    async def ticket(self, team: Team, scoring_check_category: TicketCategory) -> Ticket:
        """Create test ticket."""
        return await Ticket.objects.acreate(
            ticket_number="T035-600",
            team=team,
            category=scoring_check_category,
            title="Attachment Test",
            description="Test",
            status="open",
            discord_thread_id=5555666677778888,
        )

    async def test_attachment_saved_successfully(self, cog: TicketingCog, ticket: Ticket) -> None:
        """Test that valid attachments are saved to database."""
        from ticketing.models import TicketAttachment

        # Mock attachment
        attachment = AsyncMock(spec=discord.Attachment)
        attachment.filename = "screenshot.png"
        attachment.size = 1024 * 100  # 100KB
        attachment.content_type = "image/png"
        attachment.read = AsyncMock(return_value=b"\x89PNG\r\n\x1a\n" + b"fake image data")

        # Mock message
        message = AsyncMock(spec=discord.Message)
        message.author = Mock()
        message.author.bot = False
        message.author.id = 123456789
        message.id = 777888999000  # Add message ID
        message.author.__str__ = Mock(return_value="user#1234")
        message.channel = Mock(spec=discord.Thread)
        message.channel.id = ticket.discord_thread_id
        message.content = "Here's the screenshot"
        message.attachments = [attachment]
        message.add_reaction = AsyncMock()

        initial_count = await TicketAttachment.objects.acount()
        await cog.on_message(message)

        # Verify attachment saved
        assert await TicketAttachment.objects.acount() == initial_count + 1

        # Verify attachment data
        saved = await TicketAttachment.objects.filter(ticket=ticket).afirst()
        assert saved is not None
        assert saved.filename == "screenshot.png"
        assert saved.mime_type == "image/png"
        assert bytes(saved.file_data).startswith(b"\x89PNG")

        # Verify reaction added
        message.add_reaction.assert_called_once_with("📎")

    async def test_attachment_size_limit_enforced(self, cog: TicketingCog, ticket: Ticket) -> None:
        """Test that attachments > 10MB are rejected."""
        from ticketing.models import TicketAttachment

        # Mock large attachment (11MB)
        attachment = AsyncMock(spec=discord.Attachment)
        attachment.filename = "huge_video.mp4"
        attachment.size = 11 * 1024 * 1024  # 11MB
        attachment.content_type = "video/mp4"

        # Mock message
        message = AsyncMock(spec=discord.Message)
        message.author = Mock()
        message.author.bot = False
        message.author.id = 123456789
        message.author.mention = "<@123456789>"
        message.channel = AsyncMock(spec=discord.Thread)
        message.channel.id = ticket.discord_thread_id
        message.content = ""
        message.attachments = [attachment]

        initial_count = await TicketAttachment.objects.acount()
        await cog.on_message(message)

        # Attachment should NOT be saved
        assert await TicketAttachment.objects.acount() == initial_count

        # Error message should be sent
        message.channel.send.assert_called_once()
        args = message.channel.send.call_args
        assert "too large" in args[0][0].lower()
        assert "10mb" in args[0][0].lower()


@pytest.mark.asyncio
async def test_hostname_autocomplete_calls_quotient_off_the_event_loop() -> None:
    """A Quotient cache miss is a blocking HTTP call; it must not run on the bot's loop."""
    import asyncio
    from unittest.mock import MagicMock, patch

    def get_infrastructure() -> MagicMock:
        with pytest.raises(RuntimeError):
            asyncio.get_running_loop()  # raises only off the loop's thread
        box = MagicMock()
        box.name, box.ip = "web01", "10.0.0.5"
        return MagicMock(boxes=[box])

    client = MagicMock()
    client.get_infrastructure.side_effect = get_infrastructure
    client.get_service_choices.return_value = []

    # Patch the globals the code resolves through: under xdist a module-path patch can miss them.
    with (
        patch.dict(TicketingCog._load_infrastructure_data.__globals__, {"get_quotient_client": lambda: client}),
        patch.object(TicketingCog.archive_threads_task, "start"),
    ):
        cog = TicketingCog(MagicMock())
        choices = await cog.hostname_autocomplete(MagicMock(), "web")

    assert [c.value for c in choices] == ["web01"]
