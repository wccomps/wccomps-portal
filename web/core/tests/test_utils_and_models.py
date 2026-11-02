"""Tests for core utils and models."""

from contextlib import suppress
from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.utils import timezone
from hypothesis import given
from hypothesis import strategies as st

from core.auth_utils import get_authentik_id, permission_groups_of, team_for_groups
from core.discord_tasks import PostTicketUpdate, SyncRoles
from core.models import AuditLog, CompetitionConfig, DiscordTask
from team.models import Team

pytestmark = pytest.mark.django_db


class TestGetAuthentikGroups:
    def test_returns_groups_from_usergroups(self, blue_team_user):
        """Should extract groups from UserGroups model."""
        groups = permission_groups_of(blue_team_user)
        assert "WCComps_BlueTeam01" in groups

    def test_returns_empty_list_without_usergroups(self):
        """Should return empty list when no UserGroups exists."""
        user = User.objects.create_user(username="regular_user", password="test")
        groups = permission_groups_of(user)
        assert groups == []


class TestGetAuthentikId:
    """Tests for get_authentik_id function."""

    def test_returns_id_from_usergroups(self, blue_team_user):
        """Should extract authentik_id from UserGroups model."""
        user_id = get_authentik_id(blue_team_user)
        assert user_id is not None

    def test_returns_none_without_usergroups(self):
        """Should return None when no UserGroups exists."""
        user = User.objects.create_user(username="regular_user", password="test")
        user_id = get_authentik_id(user)
        assert user_id is None


class TestTeamForGroups:
    @pytest.fixture
    def teams(self):
        return {n: Team.objects.create(team_number=n, team_name=f"Team {n}") for n in (1, 10, 50)}

    @pytest.mark.parametrize("number", [1, 10, 50])
    def test_finds_the_team_by_its_group(self, teams, number):
        assert team_for_groups(["WCComps_Ticketing_Support", f"WCComps_BlueTeam{number:02d}"]) == teams[number]

    def test_matches_the_teams_configured_group_not_its_number(self, teams):
        Team.objects.filter(team_number=10).update(authentik_group="WCComps_BlueTeam_Renamed")

        assert team_for_groups(["WCComps_BlueTeam_Renamed"]) == teams[10]
        assert team_for_groups(["WCComps_BlueTeam10"]) is None

    @pytest.mark.parametrize(
        "groups", [[], ["WCComps_GoldTeam", "WCComps_RedTeam"], ["WCComps_BlueTeam02"], ["WCComps_BlueTeam1"]]
    )
    def test_none_without_a_teams_group(self, teams, groups):
        assert team_for_groups(groups) is None


class TestAuditLogModel:
    """Tests for AuditLog model."""

    def test_str_representation(self):
        """__str__ should return action and admin_user."""
        log = AuditLog.objects.create(
            action="clear_tickets",
            admin_user="admin_user",
            target_entity="Ticket",
            target_id=0,
            details={"count": 5},
        )
        assert str(log) == "clear_tickets by admin_user"

    def test_ordering(self):
        """Should be ordered by created_at descending."""
        log1 = AuditLog.objects.create(action="first", admin_user="user", target_entity="X", target_id=1)
        log2 = AuditLog.objects.create(action="second", admin_user="user", target_entity="X", target_id=2)

        logs = list(AuditLog.objects.all())
        assert logs[0] == log2  # Newer first
        assert logs[1] == log1


class TestDiscordTaskModel:
    """Tests for DiscordTask model."""

    def test_str_representation(self):
        """__str__ should return task_type and status."""
        task = DiscordTask.objects.create(task_type="log_to_channel", payload={"message": "hi"}, status="pending")
        assert str(task) == "log_to_channel (pending)"

    def test_default_values(self):
        """Should have correct default values."""
        task = DiscordTask.objects.create(task_type="log_to_channel", payload={"message": "hi"})
        assert task.status == "pending"
        assert task.retry_count == 0
        assert task.max_retries == 5

    def test_enqueue_takes_the_type_from_the_payload_and_the_ticket_from_its_id(self, box_reset_category):
        from ticketing.models import Ticket

        team = Team.objects.create(team_number=3, team_name="Team 03")
        ticket = Ticket.objects.create(ticket_number="T003-001", team=team, category=box_reset_category, title="t")
        update = PostTicketUpdate(ticket_id=ticket.id, action="reopened", actor="ops", reason="again")

        task = DiscordTask.enqueue(update)

        task.refresh_from_db()
        assert (task.task_type, task.status, task.ticket_id) == ("post_ticket_update", "pending", ticket.id)
        assert task.typed_payload() == update

    def test_enqueue_leaves_the_ticket_unset_for_payloads_without_one(self):
        assert DiscordTask.enqueue(SyncRoles(requested_by="ops", dry_run=True)).ticket_id is None

    def test_typed_payload_takes_a_missing_ticket_id_from_the_ticket_fk(self, box_reset_category):
        """The previous release's web left post_ticket_update's ticket on the FK only."""
        from ticketing.models import Ticket

        team = Team.objects.create(team_number=3, team_name="Team 03")
        ticket = Ticket.objects.create(ticket_number="T003-001", team=team, category=box_reset_category, title="t")
        task = DiscordTask(task_type="post_ticket_update", ticket=ticket, payload={"action": "claimed", "actor": "ops"})

        assert task.typed_payload() == PostTicketUpdate(ticket_id=ticket.id, action="claimed", actor="ops")

    def test_typed_payload_ignores_keys_the_type_does_not_declare(self):
        task = DiscordTask(task_type="sync_roles", payload={"requested_by": "ops", "dry_run": True, "progress": {}})

        assert task.typed_payload() == SyncRoles(requested_by="ops", dry_run=True)

    def test_clean_rejects_a_payload_missing_a_field(self):
        from django.core.exceptions import ValidationError

        with pytest.raises(ValidationError, match="team_number"):
            DiscordTask(task_type="setup_team_infrastructure", payload={}).clean()


class TestCompetitionConfigModel:
    """Tests for CompetitionConfig model."""

    def test_get_config_creates_singleton(self):
        """get_config should create singleton if not exists."""
        assert CompetitionConfig.objects.count() == 0

        config = CompetitionConfig.get_config()

        assert CompetitionConfig.objects.count() == 1
        assert config.pk == 1

    def test_get_config_returns_existing(self):
        """get_config should return existing config."""
        CompetitionConfig.objects.create(pk=1, max_team_members=15)

        config = CompetitionConfig.get_config()

        assert config.max_team_members == 15
        assert CompetitionConfig.objects.count() == 1

    def test_should_enable_applications(self):
        """should_enable_applications should check start time."""
        config = CompetitionConfig.get_config()

        # No start time set
        assert not config.should_enable_applications()

        # Start time in past, not yet enabled
        config.competition_start_time = timezone.now() - timedelta(hours=1)
        config.applications_enabled = False
        config.save()
        assert config.should_enable_applications()

        # Start time in past, already enabled
        config.applications_enabled = True
        config.save()
        assert not config.should_enable_applications()

        # Start time in future
        config.competition_start_time = timezone.now() + timedelta(hours=1)
        config.applications_enabled = False
        config.save()
        assert not config.should_enable_applications()

    def test_should_disable_applications(self):
        """should_disable_applications should check end time."""
        config = CompetitionConfig.get_config()

        # No end time set
        assert not config.should_disable_applications()

        # End time in past, still enabled
        config.competition_end_time = timezone.now() - timedelta(hours=1)
        config.applications_enabled = True
        config.save()
        assert config.should_disable_applications()

        # End time in past, already disabled
        config.applications_enabled = False
        config.save()
        assert not config.should_disable_applications()

        # End time in future
        config.competition_end_time = timezone.now() + timedelta(hours=1)
        config.applications_enabled = True
        config.save()
        assert not config.should_disable_applications()

    def test_str_with_start_time(self):
        """__str__ should include start time when set."""
        config = CompetitionConfig.get_config()
        config.competition_start_time = timezone.now()
        config.save()

        assert "Competition starts at" in str(config)

    def test_str_without_start_time(self):
        """__str__ should indicate not scheduled when no start time."""
        config = CompetitionConfig.get_config()
        assert str(config) == "Competition not scheduled"


@pytest.mark.django_db(transaction=True)
class TestTeamModelProperties:
    """Property-based tests for Team model using Django-Hypothesis."""

    @given(
        team_number=st.integers(min_value=1, max_value=50),
        max_members=st.integers(min_value=1, max_value=100),
    )
    def test_team_creation_with_valid_numbers(self, team_number: int, max_members: int):
        """Team can be created with valid team numbers, and its member limit is the competition's."""
        CompetitionConfig.objects.update_or_create(pk=1, defaults={"max_team_members": max_members})
        # Clean up any existing team with this number
        Team.objects.filter(team_number=team_number).delete()

        team = Team.objects.create(
            team_number=team_number,
            team_name=f"Test Team {team_number}",
        )

        assert team.team_number == team_number
        assert team.max_members == max_members
        assert team.is_active is True
        assert team.get_member_count() == 0
        assert team.is_full() is False

    @given(max_members=st.integers(min_value=1, max_value=20))
    def test_team_is_full_with_varying_capacity(self, max_members: int):
        """Team.is_full() returns correct result based on member count vs capacity."""
        # Use a unique team number for each test
        import uuid

        team_num = hash(uuid.uuid4()) % 50 + 1
        Team.objects.filter(team_number=team_num).delete()
        CompetitionConfig.objects.update_or_create(pk=1, defaults={"max_team_members": max_members})

        team = Team.objects.create(
            team_number=team_num,
            team_name=f"Capacity Test {team_num}",
        )

        # Empty team is never full
        assert team.is_full() is False
        assert team.get_member_count() == 0

        # Clean up
        team.delete()

    @given(
        team_number=st.integers(min_value=51, max_value=100),
    )
    def test_team_rejects_invalid_team_numbers(self, team_number: int):
        """Team creation should fail for team numbers outside 1-50 range."""
        from django.core.exceptions import ValidationError

        with pytest.raises(ValidationError) as exc_info:
            Team.objects.create(
                team_number=team_number,
                team_name=f"Invalid Team {team_number}",
            )

        assert "team_number" in str(exc_info.value)


class TestAdvisoryLock:
    @staticmethod
    def _session():
        from django.db import connection

        other = connection.copy()
        other.ensure_connection()
        return other

    @staticmethod
    def _query(session, sql, params=()):
        with session.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.fetchone()[0]

    def test_a_dropped_connection_ends_the_block_with_its_own_error(self):
        from django.db import OperationalError

        from core.utils import advisory_lock

        holder, other = self._session(), self._session()
        try:
            with pytest.raises(OperationalError), advisory_lock("test:dropped", conn=holder) as held:
                assert held
                pid = self._query(holder, "SELECT pg_backend_pid()")
                assert self._query(other, "SELECT pg_terminate_backend(%s, 5000)", [pid])
                self._query(holder, "SELECT 1")
            assert self._query(other, "SELECT pg_try_advisory_lock(hashtextextended('test:dropped', 0))")
        finally:
            holder.close()
            other.close()

    def test_release_in_an_aborted_transaction_raises_rather_than_hiding_that_the_lock_is_still_held(self):
        from django.db import DatabaseError, connection, transaction

        from core.utils import advisory_lock

        try:
            with (
                suppress(DatabaseError),
                transaction.atomic(),
                pytest.raises(DatabaseError, match="aborted"),
                advisory_lock("test:aborted") as held,
            ):
                assert held
                with suppress(DatabaseError), connection.cursor() as cursor:
                    cursor.execute("SELECT * FROM no_such_table")
        finally:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock_all()")

    def test_an_int_key_is_the_same_lock_as_the_transaction_lock_on_that_int(self):
        from registration.locks import SCHOOL_LIST_LOCK_ID

        from core.utils import advisory_lock

        holder, other = self._session(), self._session()
        try:
            with advisory_lock(SCHOOL_LIST_LOCK_ID, wait=True, conn=holder) as held:
                assert held
                assert not self._query(other, "SELECT pg_try_advisory_xact_lock(%s)", [SCHOOL_LIST_LOCK_ID])
            assert self._query(other, "SELECT pg_try_advisory_xact_lock(%s)", [SCHOOL_LIST_LOCK_ID])
        finally:
            holder.close()
            other.close()
