"""Tests for Discord queue processing and retry logic."""

from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
import pytest_asyncio
from asgiref.sync import sync_to_async
from django.core.exceptions import ObjectDoesNotExist
from django.utils import timezone

from bot.discord_queue import DiscordQueueProcessor, PermanentTaskError
from core.discord_tasks import PostTicketUpdate, SyncMemberRoles, SyncRoles
from core.models import DiscordTask
from team.models import Team


def stub_setup(fake: AsyncMock) -> AsyncMock:
    """Stand in for the team setup the queue runs, until the test ends."""
    patch("bot.discord_queue.setup_team_infrastructure", fake).start()
    return fake


@pytest.fixture(autouse=True)
def _stop_stubs() -> Any:
    yield
    patch.stopall()


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
        setup = stub_setup(AsyncMock(return_value=MagicMock()))

        await processor._process_task(task)

        await task.arefresh_from_db()
        assert task.status == "completed"
        setup.assert_awaited_once()
        assert setup.await_args.args[1].team_number == test_team.team_number

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

        # Create a proper Forbidden error
        mock_response = MagicMock()
        mock_response.status = 403
        mock_response.reason = "Forbidden"
        forbidden_error = discord.errors.Forbidden(mock_response, "Missing Permissions")
        stub_setup(AsyncMock(side_effect=forbidden_error))

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

        # Create a proper Forbidden error
        mock_response = MagicMock()
        mock_response.status = 403
        mock_response.reason = "Forbidden"
        forbidden_error = discord.errors.Forbidden(mock_response, "Missing Permissions")
        stub_setup(AsyncMock(side_effect=forbidden_error))

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

        # Create a proper Forbidden error
        mock_response = MagicMock()
        mock_response.status = 403
        mock_response.reason = "Forbidden"
        forbidden_error = discord.errors.Forbidden(mock_response, "Missing Permissions")
        stub_setup(AsyncMock(side_effect=forbidden_error))

        # Mock log_to_ops_channel to avoid actual Discord calls
        with patch("bot.discord_queue.log_to_ops_channel", new_callable=AsyncMock):
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

        # Create a proper Forbidden error
        mock_response = MagicMock()
        mock_response.status = 403
        mock_response.reason = "Forbidden"
        error_msg = "Missing Permissions"
        forbidden_error = discord.errors.Forbidden(mock_response, error_msg)
        stub_setup(AsyncMock(side_effect=forbidden_error))

        # Process task
        await processor._process_task(task)

        # Refresh from DB
        await task.arefresh_from_db()

        # Verify error message is captured
        assert task.error_message != ""
        assert "Forbidden" in task.error_message

    async def test_setup_failure_records_the_reason(self, mock_bot_with_guild: Any, test_team: Team) -> None:
        from bot.discord_manager import TeamInfrastructureError

        task = await DiscordTask.objects.acreate(
            task_type="setup_team_infrastructure", payload={"team_number": test_team.team_number}
        )
        processor = DiscordQueueProcessor(mock_bot_with_guild)
        stub_setup(
            AsyncMock(
                side_effect=TeamInfrastructureError("Created the role for team 30 but not its category: Missing Access")
            )
        )

        await processor._process_task(task)

        await task.arefresh_from_db()
        assert task.error_message == "Created the role for team 30 but not its category: Missing Access"

    @pytest.mark.parametrize(
        ("task_type", "payload", "reason"),
        [
            ("setup_team_infrastructure", {}, "team_number"),
            ("setup_team_infrastructure", {"team_number": "thirty"}, "team_number"),
            ("setup_team_infrastructure", [], "not a JSON object"),
            ("retired_type", {}, "Unknown task type"),
            ("log_to_channel", {"message": ""}, "Missing message"),
            ("broadcast_message", {"target": "9-3", "message": "hi", "sender": "ops"}, "Invalid team range"),
            ("post_comment", {"ticket_id": 0, "comment_id": 0}, "does not exist"),
            ("setup_team_infrastructure", {"team_number": 49}, "does not exist"),
        ],
    )
    async def test_a_task_that_can_never_succeed_fails_at_once(
        self, mock_bot_with_guild: Any, task_type: str, payload: Any, reason: str
    ) -> None:
        tasks = await DiscordTask.objects.abulk_create(
            [DiscordTask(task_type=task_type, payload=payload, status="pending", max_retries=5)]
        )
        task = tasks[0]
        processor = DiscordQueueProcessor(mock_bot_with_guild)
        processor._guild = MagicMock(return_value=MagicMock())

        with patch("bot.discord_queue.log_to_ops_channel", new_callable=AsyncMock) as ops:
            await processor._process_task(task)

        await task.arefresh_from_db()
        assert task.status == "failed"
        assert task.next_retry_at is None
        assert task.retry_count == 0
        assert reason in task.error_message
        assert reason in ops.await_args.args[1]

    async def test_a_discord_rate_limit_is_retried_like_any_other_error(
        self, mock_bot_with_guild: Any, test_team: Team
    ) -> None:
        task = await DiscordTask.objects.acreate(
            task_type="setup_team_infrastructure", payload={"team_number": test_team.team_number}
        )
        processor = DiscordQueueProcessor(mock_bot_with_guild)
        stub_setup(AsyncMock(side_effect=discord.errors.RateLimited(60)))

        await processor._process_task(task)

        await task.arefresh_from_db()
        assert (task.status, task.retry_count) == ("pending", 1)

    async def test_a_recreate_retry_keeps_what_the_first_attempt_built(
        self, mock_bot_with_guild: Any, test_team: Team
    ) -> None:
        task = await DiscordTask.objects.acreate(
            task_type="setup_team_infrastructure", payload={"team_number": test_team.team_number, "recreate": True}
        )
        processor = DiscordQueueProcessor(mock_bot_with_guild)
        processor._guild = MagicMock(return_value=MagicMock())
        setup = AsyncMock(side_effect=[RuntimeError("Discord down"), MagicMock()])

        with (
            patch("bot.discord_manager.setup_team_infrastructure", setup),
            patch("bot.discord_manager.delete_team_infrastructure", new_callable=AsyncMock) as delete,
        ):
            await processor._process_task(task)
            await DiscordTask.objects.filter(pk=task.pk).aupdate(next_retry_at=None)
            await task.arefresh_from_db()
            await processor._process_task(task)

        await task.arefresh_from_db()
        assert task.status == "completed"
        delete.assert_awaited_once()

    async def test_member_sync_for_someone_not_in_the_guild_completes_without_syncing(
        self, mock_bot_with_guild: Any
    ) -> None:
        """The periodic sync covers them once they join."""
        guild = MagicMock()
        guild.get_member.return_value = None
        guild.fetch_member = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404), "unknown member"))
        processor = DiscordQueueProcessor(mock_bot_with_guild)
        processor._guild = MagicMock(return_value=guild)

        with patch("bot.discord_queue.sync_member_roles", new_callable=AsyncMock) as sync:
            await processor._handle_sync_member_roles(SyncMemberRoles(discord_id=999999999))

        sync.assert_not_awaited()

    async def test_member_sync_runs_for_a_member_in_the_guild(self, mock_bot_with_guild: Any) -> None:
        guild, member = MagicMock(), MagicMock()
        guild.get_member.return_value = member
        processor = DiscordQueueProcessor(mock_bot_with_guild)
        processor._guild = MagicMock(return_value=guild)
        result = {"roles_added": 1, "roles_removed": 0, "errors": 0, "changes": []}

        with patch("bot.discord_queue.sync_member_roles", new=AsyncMock(return_value=result)) as sync:
            await processor._handle_sync_member_roles(SyncMemberRoles(discord_id=42))

        sync.assert_awaited_once_with(guild, member)


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
class TestDiscordQueueOrdering:
    """Test task processing order and queue management."""

    async def test_tasks_processed_in_created_at_order(self, mock_bot_with_guild: Any) -> None:
        """Test that tasks are fetched and processed in created_at order."""
        processor = DiscordQueueProcessor(mock_bot_with_guild)

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
            "bot.discord_queue.log_to_ops_channel",
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

        with patch("bot.discord_queue.log_to_ops_channel", new_callable=AsyncMock):
            await processor._process_pending_tasks()

        await task_ready.arefresh_from_db()
        await task_future.arefresh_from_db()

        # Only ready task should be completed
        assert task_ready.status == "completed"
        assert task_future.status == "pending"

    async def test_max_ten_tasks_per_poll(self, mock_bot_with_guild: Any) -> None:
        """Test that only max 10 tasks are fetched per poll."""
        processor = DiscordQueueProcessor(mock_bot_with_guild)

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

        with patch("bot.discord_queue.log_to_ops_channel", new_callable=AsyncMock):
            await processor._process_pending_tasks()

        # Count completed tasks - should be max 10
        completed = await DiscordTask.objects.filter(status="completed").acount()
        assert completed == 10


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
class TestExponentialBackoff:
    """Test exponential backoff retry timing edge cases."""

    async def test_exponential_backoff_formula(self, mock_bot_with_guild: Any, test_team: Team) -> None:
        """Test exponential backoff follows 2^retry_count formula."""
        processor = DiscordQueueProcessor(mock_bot_with_guild)
        stub_setup(AsyncMock(side_effect=Exception("Network error")))

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
                payload={"discord_id": 111111111, "team_number": test_team.team_number},
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

    async def test_exponential_backoff_capped_at_300_seconds(self, mock_bot_with_guild: Any, test_team: Team) -> None:
        """Test that exponential backoff is capped at 300 seconds (5 minutes)."""
        processor = DiscordQueueProcessor(mock_bot_with_guild)
        stub_setup(AsyncMock(side_effect=Exception("Network error")))

        # Create task with high retry count where 2^retry_count > 300
        task = await DiscordTask.objects.acreate(
            task_type="setup_team_infrastructure",
            payload={"discord_id": 111111111, "team_number": test_team.team_number},
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
        stub_setup(AsyncMock(side_effect=Exception("Network error")))

        # Create task at max retries
        task = await DiscordTask.objects.acreate(
            task_type="setup_team_infrastructure",
            payload={"discord_id": 111111111, "team_number": team.team_number},
            status="pending",
            retry_count=5,  # At max_retries
            max_retries=5,
        )

        # Mock log_to_ops_channel to avoid Discord calls
        with patch("bot.discord_queue.log_to_ops_channel", new_callable=AsyncMock):
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
        stub_setup(AsyncMock(side_effect=Exception("Critical error")))

        task = await DiscordTask.objects.acreate(
            task_type="setup_team_infrastructure",
            payload={"discord_id": 111111111, "team_number": team.team_number},
            status="pending",
            retry_count=5,
            max_retries=5,
        )

        with patch("bot.discord_queue.log_to_ops_channel", new_callable=AsyncMock) as mock_log:
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
        patch("bot.discord_queue.sync_roles", new=AsyncMock(return_value=stats)),
        patch("bot.discord_queue.log_to_ops_channel", new=AsyncMock()),
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


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
class TestTaskDeletedWhileRunning:
    """Deleting a ticket deletes its tasks, possibly while one of them runs."""

    @pytest.mark.parametrize(
        "outcome", [None, PermanentTaskError("bad"), ObjectDoesNotExist("gone"), RuntimeError("discord down")]
    )
    async def test_the_batch_goes_on(self, box_reset_category: Any, outcome: Exception | None) -> None:
        from ticketing.models import Ticket

        team = await Team.objects.acreate(team_number=29, team_name="Team 29")
        ticket = await Ticket.objects.acreate(
            ticket_number="T029-001", team=team, category=box_reset_category, title="t"
        )
        doomed = await DiscordTask.objects.acreate(
            task_type="post_ticket_update",
            ticket=ticket,
            payload={"ticket_id": ticket.id, "action": "claimed", "actor": "v"},
        )
        later = await DiscordTask.objects.acreate(task_type="log_to_channel", payload={"message": "next"})
        processor = DiscordQueueProcessor(MagicMock())

        async def dispatch(payload: Any, *_progress: Any) -> None:
            if isinstance(payload, PostTicketUpdate):
                await ticket.adelete()
                if outcome:
                    raise outcome

        with (
            patch.object(processor, "_dispatch", side_effect=dispatch),
            patch("bot.discord_queue.log_to_ops_channel", new_callable=AsyncMock) as ops,
        ):
            await processor._process_pending_tasks()

        assert not await DiscordTask.objects.filter(pk=doomed.pk).aexists()
        await later.arefresh_from_db()
        assert later.status == "completed", later.error_message
        ops.assert_not_awaited()
