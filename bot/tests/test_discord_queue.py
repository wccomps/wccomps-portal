"""Tests for Discord queue processing and retry logic."""

from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
import pytest_asyncio
from asgiref.sync import sync_to_async
from django.utils import timezone

from bot.discord_queue import DiscordQueueProcessor
from core.discord_tasks import SyncMemberRoles, SyncRoles
from core.models import DiscordTask
from team.models import Team


@pytest_asyncio.fixture
async def test_team(db: Any) -> Team:
    """Create or get test team."""
    team, _ = await Team.objects.aget_or_create(
        team_number=30,
        defaults={
            "team_name": "Test Team",
            "authentik_group": "WCComps_BlueTeam01",
            "discord_role_id": 1001,
            "discord_category_id": 2001,
        },
    )
    return team


@pytest.fixture
def mock_bot_with_guild() -> Any:
    """Create a mock Discord bot with a guild and member."""
    bot = AsyncMock(spec=discord.Client)

    # Mock guild
    guild = MagicMock(spec=discord.Guild)
    guild.id = 525435725123158026
    guild.name = "Test Guild"

    # Mock member
    member = MagicMock(spec=discord.Member)
    member.id = 111111111
    member.name = "testmember"
    member.roles = []

    # Setup guild methods
    def get_member(member_id: int) -> Any:
        if member_id == 111111111:
            return member
        return None

    guild.get_member = MagicMock(side_effect=get_member)

    # Setup bot
    bot.guilds = [guild]

    return bot


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
class TestTaskRetry:
    """Retry logic, shown with setup_team_infrastructure tasks."""

    async def test_task_succeeds_on_first_try(self, mock_bot_with_guild: Any, test_team: Team) -> None:
        task = await DiscordTask.objects.acreate(
            task_type="setup_team_infrastructure",
            payload={"team_number": test_team.team_number},
            status="pending",
            retry_count=0,
            max_retries=5,
        )
        processor = DiscordQueueProcessor(mock_bot_with_guild)
        processor._manager = MagicMock(return_value=AsyncMock())
        processor._manager.return_value.setup_team_infrastructure = AsyncMock(return_value=(MagicMock(), MagicMock()))

        await processor._process_task(task)

        await task.arefresh_from_db()
        assert task.status == "completed"
        processor._manager.return_value.setup_team_infrastructure.assert_awaited_once_with(test_team.team_number)

    async def test_task_retries_on_discord_error(self, mock_bot_with_guild: Any, test_team: Team) -> None:
        """A task retries on a Discord API error."""
        # Create task with team number from fixture
        task = await DiscordTask.objects.acreate(
            task_type="setup_team_infrastructure",
            payload={"discord_id": 111111111, "team_number": test_team.team_number},
            status="pending",
            retry_count=0,
            max_retries=5,
        )

        # Create processor
        processor = DiscordQueueProcessor(mock_bot_with_guild)
        processor._manager = MagicMock(return_value=AsyncMock())

        # Create a proper Forbidden error
        mock_response = MagicMock()
        mock_response.status = 403
        mock_response.reason = "Forbidden"
        forbidden_error = discord.errors.Forbidden(mock_response, "Missing Permissions")
        processor._manager.return_value.setup_team_infrastructure = AsyncMock(side_effect=forbidden_error)

        # Process task
        await processor._process_task(task)

        # Refresh task from DB
        await task.arefresh_from_db()

        # Verify retry logic
        assert task.retry_count == 1
        assert task.status == "pending"
        assert task.next_retry_at is not None
        assert "Forbidden" in task.error_message or len(task.error_message) > 0

    async def test_task_exponential_backoff_timing(self, mock_bot_with_guild: Any, test_team: Team) -> None:
        """Test exponential backoff timing: 2s, 4s, 8s, 16s."""
        processor = DiscordQueueProcessor(mock_bot_with_guild)
        processor._manager = MagicMock(return_value=AsyncMock())

        # Create a proper Forbidden error
        mock_response = MagicMock()
        mock_response.status = 403
        mock_response.reason = "Forbidden"
        forbidden_error = discord.errors.Forbidden(mock_response, "Missing Permissions")
        processor._manager.return_value.setup_team_infrastructure = AsyncMock(side_effect=forbidden_error)

        # Test only up to retry_count=3 (4th attempt) to avoid hitting max_retries
        expected_backoffs = [2, 4, 8, 16]  # 2^1, 2^2, 2^3, 2^4

        for retry_attempt, expected_backoff in enumerate(expected_backoffs, start=1):
            # Create new task for each retry
            task = await DiscordTask.objects.acreate(
                task_type="setup_team_infrastructure",
                payload={"discord_id": 111111111, "team_number": test_team.team_number},
                status="pending",
                retry_count=retry_attempt - 1,
                max_retries=5,
            )

            now = timezone.now()

            # Process task
            await processor._process_task(task)

            # Refresh from DB
            await task.arefresh_from_db()

            # Verify retry count
            assert task.retry_count == retry_attempt

            # Verify status is pending (retrying)
            assert task.status == "pending"

            # Verify next retry time
            assert task.next_retry_at is not None

            # Check backoff timing (allowing 1 second tolerance)
            time_diff = (task.next_retry_at - now).total_seconds()
            assert abs(time_diff - expected_backoff) <= 1.0, (
                f"Retry {retry_attempt}: expected {expected_backoff}s, got {time_diff}s"
            )

    async def test_task_fails_after_max_retries(self, mock_bot_with_guild: Any, test_team: Team) -> None:
        """Test that task is marked failed after max retries exceeded."""
        # Create task with max retries already reached
        task = await DiscordTask.objects.acreate(
            task_type="setup_team_infrastructure",
            payload={"discord_id": 111111111, "team_number": test_team.team_number},
            status="pending",
            retry_count=5,  # Already at max
            max_retries=5,
        )

        processor = DiscordQueueProcessor(mock_bot_with_guild)
        processor._manager = MagicMock(return_value=AsyncMock())

        # Create a proper Forbidden error
        mock_response = MagicMock()
        mock_response.status = 403
        mock_response.reason = "Forbidden"
        forbidden_error = discord.errors.Forbidden(mock_response, "Missing Permissions")
        processor._manager.return_value.setup_team_infrastructure = AsyncMock(side_effect=forbidden_error)

        # Mock log_to_ops_channel to avoid actual Discord calls
        with patch("bot.utils.log_to_ops_channel", new_callable=AsyncMock):
            # Process task
            await processor._process_task(task)

        # Refresh from DB
        await task.arefresh_from_db()

        # Verify task is marked as failed
        assert task.status == "failed"
        assert task.retry_count == 6
        assert "Forbidden" in task.error_message

    async def test_task_captures_error_message(self, mock_bot_with_guild: Any, test_team: Team) -> None:
        """Test that error messages are captured in task."""
        task = await DiscordTask.objects.acreate(
            task_type="setup_team_infrastructure",
            payload={"discord_id": 111111111, "team_number": test_team.team_number},
            status="pending",
            retry_count=0,
            max_retries=5,
        )

        processor = DiscordQueueProcessor(mock_bot_with_guild)
        processor._manager = MagicMock(return_value=AsyncMock())

        # Create a proper Forbidden error
        mock_response = MagicMock()
        mock_response.status = 403
        mock_response.reason = "Forbidden"
        error_msg = "Missing Permissions"
        forbidden_error = discord.errors.Forbidden(mock_response, error_msg)
        processor._manager.return_value.setup_team_infrastructure = AsyncMock(side_effect=forbidden_error)

        # Process task
        await processor._process_task(task)

        # Refresh from DB
        await task.arefresh_from_db()

        # Verify error message is captured
        assert task.error_message != ""
        assert "Forbidden" in task.error_message

    async def test_task_rate_limit_uses_discord_retry_after(self, mock_bot_with_guild: Any, test_team: Team) -> None:
        """Test that rate limit errors use Discord's retry_after value."""
        task = await DiscordTask.objects.acreate(
            task_type="setup_team_infrastructure",
            payload={"discord_id": 111111111, "team_number": test_team.team_number},
            status="pending",
            retry_count=0,
            max_retries=5,
        )

        processor = DiscordQueueProcessor(mock_bot_with_guild)
        processor._manager = MagicMock(return_value=AsyncMock())

        # Create rate limit error with retry_after
        rate_limit_error = discord.errors.RateLimited(60)  # 60 second retry
        processor._manager.return_value.setup_team_infrastructure = AsyncMock(side_effect=rate_limit_error)

        now = timezone.now()

        # Process task
        await processor._process_task(task)

        # Refresh from DB
        await task.arefresh_from_db()

        # Verify rate limit handling
        assert task.retry_count == 1
        assert task.status == "pending"
        assert task.next_retry_at is not None
        assert "rate limited" in task.error_message.lower() or "Rate limited" in task.error_message

        # Verify retry_after is respected (allowing 2 second tolerance)
        time_diff = (task.next_retry_at - now).total_seconds()
        assert abs(time_diff - 60) <= 2.0, f"Expected ~60s retry delay, got {time_diff}s"

    async def test_task_missing_payload_fields(self, mock_bot_with_guild: Any, test_team: Team) -> None:
        """Test handling of missing payload fields - retries then fails."""
        tasks = await sync_to_async(DiscordTask.objects.bulk_create)(
            [
                DiscordTask(
                    task_type="setup_team_infrastructure",
                    payload={},  # Missing team_number
                    status="pending",
                    retry_count=0,
                    max_retries=5,
                )
            ]
        )
        task = tasks[0]

        processor = DiscordQueueProcessor(mock_bot_with_guild)
        processor._manager = MagicMock(return_value=AsyncMock())

        # Process task
        await processor._process_task(task)

        # Refresh from DB
        await task.arefresh_from_db()

        # Verify task is scheduled for retry (not immediately failed)
        assert task.status == "pending"
        assert task.retry_count == 1
        assert task.next_retry_at is not None
        assert "team_number" in task.error_message

    async def test_member_sync_for_someone_not_in_the_guild_completes_without_syncing(
        self, mock_bot_with_guild: Any
    ) -> None:
        """The periodic sync covers them once they join."""
        guild = MagicMock()
        guild.get_member.return_value = None
        guild.fetch_member = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404), "unknown member"))
        processor = DiscordQueueProcessor(mock_bot_with_guild)
        processor._guild = MagicMock(return_value=guild)

        with patch("bot.role_sync.sync_member_roles", new_callable=AsyncMock) as sync:
            await processor._handle_sync_member_roles(SyncMemberRoles(discord_id=999999999))

        sync.assert_not_awaited()

    async def test_member_sync_runs_for_a_member_in_the_guild(self, mock_bot_with_guild: Any) -> None:
        guild, member = MagicMock(), MagicMock()
        guild.get_member.return_value = member
        processor = DiscordQueueProcessor(mock_bot_with_guild)
        processor._guild = MagicMock(return_value=guild)
        result = {"roles_added": 1, "roles_removed": 0, "errors": 0, "changes": []}

        with patch("bot.role_sync.sync_member_roles", new=AsyncMock(return_value=result)) as sync:
            await processor._handle_sync_member_roles(SyncMemberRoles(discord_id=42))

        sync.assert_awaited_once_with(guild, member)


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
class TestDiscordQueueOrdering:
    """Test task processing order and queue management."""

    async def test_tasks_processed_in_created_at_order(self, mock_bot_with_guild: Any) -> None:
        """Test that tasks are fetched and processed in created_at order."""
        processor = DiscordQueueProcessor(mock_bot_with_guild)
        processor._manager = MagicMock(return_value=AsyncMock())

        # created_at is auto_now_add: bulk_create stamps each row in list order.
        from core.models import DiscordTask as TaskModel

        task1 = TaskModel(
            task_type="log_to_channel",
            payload={"message": "First task"},
            status="pending",
        )
        task2 = TaskModel(
            task_type="log_to_channel",
            payload={"message": "Second task"},
            status="pending",
        )
        task3 = TaskModel(
            task_type="log_to_channel",
            payload={"message": "Third task"},
            status="pending",
        )

        await TaskModel.objects.abulk_create([task1, task2, task3])

        # Track processing order
        processed_ids = []

        async def track_processing(bot: Any, message: str) -> None:
            """Track which tasks are processed."""
            if "First" in message:
                processed_ids.append("first")
            elif "Second" in message:
                processed_ids.append("second")
            elif "Third" in message:
                processed_ids.append("third")

        with patch(
            "bot.utils.log_to_ops_channel",
            side_effect=track_processing,
        ):
            await processor._process_pending_tasks()

        # Verify tasks processed in order
        assert processed_ids == ["first", "second", "third"]

        # Verify all completed
        await task1.arefresh_from_db()
        await task2.arefresh_from_db()
        await task3.arefresh_from_db()

        assert task1.status == "completed"
        assert task2.status == "completed"
        assert task3.status == "completed"

    async def test_only_ready_tasks_processed(self, mock_bot_with_guild: Any) -> None:
        """Test that tasks not ready for retry are skipped."""
        processor = DiscordQueueProcessor(mock_bot_with_guild)
        processor._manager = MagicMock(return_value=AsyncMock())

        now = timezone.now()

        # Create a pending task (ready - no next_retry_at)
        task_ready = await DiscordTask.objects.acreate(
            task_type="log_to_channel",
            payload={"message": "Ready task"},
            status="pending",
            created_at=now,
        )

        # Create a task scheduled for future
        task_future = await DiscordTask.objects.acreate(
            task_type="log_to_channel",
            payload={"message": "Future task"},
            status="pending",
            next_retry_at=now + timedelta(seconds=60),
            created_at=now + timedelta(seconds=1),
        )

        with patch("bot.utils.log_to_ops_channel", new_callable=AsyncMock):
            await processor._process_pending_tasks()

        await task_ready.arefresh_from_db()
        await task_future.arefresh_from_db()

        # Only ready task should be completed
        assert task_ready.status == "completed"
        assert task_future.status == "pending"

    async def test_max_ten_tasks_per_poll(self, mock_bot_with_guild: Any) -> None:
        """Test that only max 10 tasks are fetched per poll."""
        processor = DiscordQueueProcessor(mock_bot_with_guild)
        processor._manager = MagicMock(return_value=AsyncMock())

        now = timezone.now()

        # Delete any existing tasks from previous tests
        await DiscordTask.objects.all().adelete()

        # Create 15 pending tasks
        for i in range(15):
            await DiscordTask.objects.acreate(
                task_type="log_to_channel",
                payload={"message": f"Task {i}"},
                status="pending",
                created_at=now + timedelta(seconds=i),
            )

        with patch("bot.utils.log_to_ops_channel", new_callable=AsyncMock):
            await processor._process_pending_tasks()

        # Count completed tasks - should be max 10
        completed = await DiscordTask.objects.filter(status="completed").acount()
        assert completed == 10


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
class TestExponentialBackoff:
    """Test exponential backoff retry timing edge cases."""

    async def test_exponential_backoff_formula(self, mock_bot_with_guild: Any) -> None:
        """Test exponential backoff follows 2^retry_count formula."""
        processor = DiscordQueueProcessor(mock_bot_with_guild)
        processor._manager = MagicMock(return_value=AsyncMock())
        processor._manager.return_value.setup_team_infrastructure = AsyncMock(side_effect=Exception("Network error"))

        # Test multiple retry levels: after incrementing retry_count, backoff is 2^retry_count
        test_cases = [
            (0, 1),  # retry_count=0 -> increments to 1 -> 2^1 = 2 seconds
            (1, 2),  # retry_count=1 -> increments to 2 -> 2^2 = 4 seconds
            (2, 3),  # retry_count=2 -> increments to 3 -> 2^3 = 8 seconds
            (3, 4),  # retry_count=3 -> increments to 4 -> 2^4 = 16 seconds
        ]

        for retry_count, expected_exponent in test_cases:
            task = await DiscordTask.objects.acreate(
                task_type="setup_team_infrastructure",
                payload={"discord_id": 111111111, "team_number": 1},
                status="pending",
                retry_count=retry_count,
                max_retries=5,
            )

            now = timezone.now()
            await processor._process_task(task)

            await task.arefresh_from_db()

            expected_backoff = 2**expected_exponent
            time_diff = (task.next_retry_at - now).total_seconds()

            # Allow 1 second tolerance
            assert expected_backoff - 1 <= time_diff <= expected_backoff + 1, (
                f"retry_count={retry_count}: expected {expected_backoff}s, got {time_diff}s"
            )

    async def test_exponential_backoff_capped_at_300_seconds(self, mock_bot_with_guild: Any) -> None:
        """Test that exponential backoff is capped at 300 seconds (5 minutes)."""
        processor = DiscordQueueProcessor(mock_bot_with_guild)
        processor._manager = MagicMock(return_value=AsyncMock())
        processor._manager.return_value.setup_team_infrastructure = AsyncMock(side_effect=Exception("Network error"))

        # Create task with high retry count where 2^retry_count > 300
        task = await DiscordTask.objects.acreate(
            task_type="setup_team_infrastructure",
            payload={"discord_id": 111111111, "team_number": 1},
            status="pending",
            retry_count=8,  # Will be incremented to 9, 2^9 = 512 > 300
            max_retries=10,
        )

        now = timezone.now()
        await processor._process_task(task)

        await task.arefresh_from_db()

        # Should be capped at 300
        time_diff = (task.next_retry_at - now).total_seconds()
        assert 299 <= time_diff <= 301, f"Expected ~300s, got {time_diff}s"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
class TestPermanentFailure:
    """Test permanent task failure after max retries."""

    async def test_fails_permanently_after_max_retries(self, mock_bot_with_guild: Any) -> None:
        """Test task is marked failed when retry_count reaches max_retries."""
        from team.models import Team

        # Create team needed for the task
        team = await Team.objects.acreate(
            team_number=31,
            team_name="Test Team",
            authentik_group="WCComps_BlueTeam01",
            discord_role_id=1001,
            discord_category_id=2001,
        )

        processor = DiscordQueueProcessor(mock_bot_with_guild)
        processor._manager = MagicMock(return_value=AsyncMock())
        processor._manager.return_value.setup_team_infrastructure = AsyncMock(side_effect=Exception("Network error"))

        # Create task at max retries
        task = await DiscordTask.objects.acreate(
            task_type="setup_team_infrastructure",
            payload={"discord_id": 111111111, "team_number": team.team_number},
            status="pending",
            retry_count=5,  # At max_retries
            max_retries=5,
        )

        # Mock log_to_ops_channel to avoid Discord calls
        with patch("bot.utils.log_to_ops_channel", new_callable=AsyncMock):
            await processor._process_task(task)

        await task.arefresh_from_db()

        # Verify permanent failure
        assert task.status == "failed"
        assert task.retry_count == 6  # Incremented once more
        assert "Network error" in task.error_message

    async def test_logs_to_ops_channel_on_permanent_failure(self, mock_bot_with_guild: Any) -> None:
        """Test that permanent failures are logged to ops channel."""
        from team.models import Team

        # Create team needed for the task
        team = await Team.objects.acreate(
            team_number=32,
            team_name="Test Team 2",
            authentik_group="WCComps_BlueTeam02",
            discord_role_id=1002,
            discord_category_id=2002,
        )

        processor = DiscordQueueProcessor(mock_bot_with_guild)
        processor._manager = MagicMock(return_value=AsyncMock())
        processor._manager.return_value.setup_team_infrastructure = AsyncMock(side_effect=Exception("Critical error"))

        task = await DiscordTask.objects.acreate(
            task_type="setup_team_infrastructure",
            payload={"discord_id": 111111111, "team_number": team.team_number},
            status="pending",
            retry_count=5,
            max_retries=5,
        )

        with patch("bot.utils.log_to_ops_channel", new_callable=AsyncMock) as mock_log:
            await processor._process_task(task)

            # Verify log_to_ops_channel was called
            mock_log.assert_called_once()
            call_args = mock_log.call_args
            assert "failed" in call_args[0][1].lower()
            assert "Critical error" in call_args[0][1]


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
class TestStrandedTasks:
    """A bot killed mid-task leaves its row in "processing"; the next bot recovers it."""

    async def test_recent_stranded_task_is_requeued_and_old_one_failed(self) -> None:
        recent = await DiscordTask.objects.acreate(
            task_type="log_to_channel", payload={"message": "hi"}, status="processing"
        )
        old = await DiscordTask.objects.acreate(
            task_type="log_to_channel", payload={"message": "old"}, status="processing"
        )
        await DiscordTask.objects.filter(pk=old.pk).aupdate(created_at=timezone.now() - timedelta(days=300))

        await DiscordQueueProcessor._recover_stranded_tasks()

        await recent.arefresh_from_db()
        await old.arefresh_from_db()
        assert recent.status == "pending"
        assert old.status == "failed"

    async def test_task_claimed_elsewhere_is_not_handled(self) -> None:
        task = await DiscordTask.objects.acreate(task_type="log_to_channel", payload={"message": "hi"})
        await DiscordTask.objects.filter(pk=task.pk).aupdate(status="processing")  # another processor won
        handler = AsyncMock()
        processor = DiscordQueueProcessor(MagicMock())

        with patch.object(processor, "_dispatch", handler):
            await processor._process_task(task)

        handler.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_handler_return_is_stored_as_result_and_payload_is_left_alone() -> None:
    task = await sync_to_async(DiscordTask.enqueue)(SyncRoles(requested_by="ops", dry_run=True))
    stats = {
        "roles_added": 2,
        "roles_removed": 0,
        "errors": 0,
        "changes": ["+ alice"],
    }

    with (
        patch("bot.role_sync.competition_guild", return_value=MagicMock()),
        patch("bot.role_sync.sync_roles", new=AsyncMock(return_value=stats)),
        patch("bot.utils.log_to_ops_channel", new=AsyncMock()),
    ):
        await DiscordQueueProcessor(MagicMock())._process_task(task)

    await task.arefresh_from_db()
    assert task.status == "completed"
    assert task.payload == {"requested_by": "ops", "dry_run": True}
    assert task.result["roles_added"] == 2
    assert task.result["changes"] == ["+ alice"]


class TestCompetitionGuildLookup:
    def test_each_task_gets_the_current_guild_object(self, settings: Any) -> None:
        """A gateway re-identify replaces the Guild object; a copy taken at startup would go stale."""
        settings.COMPETITION_GUILD_ID = 525
        before, after = MagicMock(), MagicMock()
        bot = MagicMock()
        bot.get_guild.side_effect = [before, after]
        processor = DiscordQueueProcessor(bot)

        assert (processor._guild(), processor._guild()) == (before, after)
        bot.get_guild.assert_called_with(525)

    def test_a_missing_competition_guild_fails_instead_of_using_another_guild(self, settings: Any) -> None:
        settings.COMPETITION_GUILD_ID = 525
        bot = MagicMock()
        bot.get_guild.return_value = None
        bot.guilds = [MagicMock(name="volunteer guild")]

        with pytest.raises(RuntimeError, match="Competition guild not found"):
            DiscordQueueProcessor(bot)._guild()


async def _thread_task(test_team: Team) -> tuple[DiscordTask, object]:
    from core.discord_tasks import TicketCreatedWeb
    from ticketing.models import Ticket, TicketCategory

    category = await TicketCategory.objects.acreate(pk=97, display_name="Other", sort_order=97)
    ticket = await Ticket.objects.acreate(ticket_number="T030-001", team=test_team, category=category, title="t")
    return await sync_to_async(DiscordTask.enqueue)(TicketCreatedWeb(ticket_id=ticket.pk)), ticket


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("failure", ["no guild", "thread error"])
async def test_a_ticket_whose_thread_fails_is_retried_and_still_reaches_the_dashboard(
    failure: str, test_team: Team
) -> None:
    """The queue's retry is the only one a ticket's thread gets."""
    task, _ticket = await _thread_task(test_team)
    bot = MagicMock()
    bot.get_guild.return_value = None if failure == "no guild" else MagicMock()
    error = discord.HTTPException(MagicMock(status=500, reason="x"), "x")

    with (
        patch("bot.discord_queue.team_chat_channel", return_value=MagicMock(threads=[])),
        patch("bot.discord_queue.create_ticket_thread", AsyncMock(side_effect=error)),
        patch("bot.discord_queue.trigger_dashboard") as dashboard,
    ):
        await DiscordQueueProcessor(bot)._process_task(task)

    await task.arefresh_from_db()
    assert (task.status, task.retry_count) == ("pending", 1)
    dashboard.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_a_team_without_a_chat_channel_tells_ops_once_instead_of_retrying(test_team: Team) -> None:
    task, _ticket = await _thread_task(test_team)

    with (
        patch("bot.discord_queue.team_chat_channel", return_value=None),
        patch("bot.discord_queue.create_ticket_thread") as make_thread,
        patch("bot.utils.log_to_ops_channel", new_callable=AsyncMock) as ops,
        patch("bot.discord_queue.trigger_dashboard"),
    ):
        await DiscordQueueProcessor(MagicMock())._process_task(task)

    await task.arefresh_from_db()
    assert task.status == "completed"
    make_thread.assert_not_called()
    assert "T030-001" in ops.await_args.args[1]


def _bot_thread(bot: MagicMock, thread_id: int, name: str) -> MagicMock:
    thread = MagicMock(spec=discord.Thread, id=thread_id, owner_id=bot.user.id)
    thread.name = name
    return thread


async def _run_thread_task(bot: MagicMock, task: DiscordTask, threads: list[MagicMock]) -> tuple[MagicMock, MagicMock]:
    with (
        patch("bot.discord_queue.team_chat_channel", return_value=MagicMock(threads=threads)),
        patch("bot.discord_queue.create_ticket_thread", new_callable=AsyncMock) as make_thread,
        patch.object(DiscordQueueProcessor, "_holds_ticket_message", AsyncMock(return_value=False)),
        patch("bot.discord_queue.post_ticket_message", new_callable=AsyncMock) as post,
        patch("bot.discord_queue.trigger_dashboard"),
    ):
        await DiscordQueueProcessor(bot)._process_task(task)
    return make_thread, post


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("rerun", ["retry", "restart"])
async def test_a_rerun_adopts_the_thread_an_earlier_run_made_before_it_could_store_it(
    rerun: str, test_team: Team
) -> None:
    """A restart, or a failure between creating the thread and storing its id, must not make a second thread."""
    task, ticket = await _thread_task(test_team)
    if rerun == "retry":
        await DiscordTask.objects.filter(pk=task.pk).aupdate(retry_count=1)
    else:
        await DiscordTask.objects.filter(pk=task.pk).aupdate(status="processing")
        await DiscordQueueProcessor._recover_stranded_tasks()
    bot = MagicMock()
    made = discord.utils.time_snowflake(ticket.created_at + timedelta(seconds=5))
    earlier = _bot_thread(bot, made, "T030-001: Team 30, t")
    other = _bot_thread(bot, made + 1, "T030-002: Team 30, t")
    bot.get_channel.return_value = earlier

    make_thread, post = await _run_thread_task(bot, await DiscordTask.objects.aget(pk=task.pk), [other, earlier])

    await ticket.arefresh_from_db()
    assert ticket.discord_thread_id == earlier.id
    make_thread.assert_not_called()
    assert post.await_args.args[0] is earlier


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("thread_was", ["adopted", "stored"])
async def test_a_rerun_adds_the_team_to_its_thread(thread_was: str, test_team: Team) -> None:
    """Ticket threads are private: a run cut short before its member adds left the team unable to see its ticket."""
    task, ticket = await _thread_task(test_team)
    await DiscordTask.objects.filter(pk=task.pk).aupdate(retry_count=1)
    bot = MagicMock()
    thread = _bot_thread(bot, discord.utils.time_snowflake(ticket.created_at), "T030-001: Team 30, t")
    thread.add_user = AsyncMock()
    if thread_was == "stored":
        from ticketing.models import Ticket

        await Ticket.objects.filter(pk=ticket.pk).aupdate(discord_thread_id=thread.id)
    bot.get_channel.return_value = thread
    members = {11: MagicMock(name="member 11"), 12: MagicMock(name="member 12")}
    bot.get_guild.return_value.get_member.side_effect = members.get

    with patch("bot.utils.get_team_member_discord_ids", AsyncMock(return_value=[11, 12])):
        await _run_thread_task(bot, await DiscordTask.objects.aget(pk=task.pk), [thread])

    assert [c.args[0] for c in thread.add_user.await_args_list] == [members[11], members[12]]


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_a_rerun_adopts_a_thread_whose_clock_ran_a_little_behind_the_ticket_s(test_team: Team) -> None:
    """Discord's clock and the portal's differ by seconds, not the days between events."""
    task, ticket = await _thread_task(test_team)
    await DiscordTask.objects.filter(pk=task.pk).aupdate(retry_count=1)
    bot = MagicMock()
    earlier = _bot_thread(
        bot, discord.utils.time_snowflake(ticket.created_at - timedelta(seconds=30)), "T030-001: Team 30, t"
    )
    bot.get_channel.return_value = earlier

    make_thread, _post = await _run_thread_task(bot, await DiscordTask.objects.aget(pk=task.pk), [earlier])

    make_thread.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_a_rerun_leaves_an_older_ticket_s_thread_with_the_same_number_alone(test_team: Team) -> None:
    """The ticket counter restarts each event: a practice round's T030-001 thread is not this T030-001's."""
    task, ticket = await _thread_task(test_team)
    await DiscordTask.objects.filter(pk=task.pk).aupdate(retry_count=1)
    bot = MagicMock()
    practice = _bot_thread(
        bot, discord.utils.time_snowflake(ticket.created_at - timedelta(days=2)), "T030-001: Team 30, practice"
    )

    make_thread, _post = await _run_thread_task(bot, await DiscordTask.objects.aget(pk=task.pk), [practice])

    await ticket.arefresh_from_db()
    assert ticket.discord_thread_id is None
    make_thread.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_a_first_run_makes_its_own_thread(test_team: Team) -> None:
    """Only a rerun can have made a thread already."""
    task, ticket = await _thread_task(test_team)
    bot = MagicMock()
    look_alike = _bot_thread(
        bot, discord.utils.time_snowflake(ticket.created_at + timedelta(seconds=5)), "T030-001: Team 30, t"
    )

    make_thread, _post = await _run_thread_task(bot, task, [look_alike])

    make_thread.assert_awaited_once()
