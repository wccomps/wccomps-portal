"""Tests for management commands."""

from io import StringIO

import pytest
from django.contrib.auth.models import User
from django.core.management import call_command

from core.competition_utils import wipe_competition_data
from core.models import DiscordTask
from team.models import DiscordLink, LinkAttempt, LinkToken, Team
from ticketing.models import Ticket, TicketCategory, TicketHistory

pytestmark = pytest.mark.django_db


class TestInitTeamsCommand:
    """Tests for init_teams management command."""

    def test_creates_50_teams(self):
        """Command should create 50 teams."""
        assert Team.objects.count() == 0

        out = StringIO()
        call_command("init_teams", stdout=out)

        assert Team.objects.count() == 50
        assert "Initialization complete: 50 created" in out.getvalue()

    def test_idempotent_running_twice(self):
        """Running twice should not create duplicates."""
        call_command("init_teams", stdout=StringIO())
        assert Team.objects.count() == 50

        out = StringIO()
        call_command("init_teams", stdout=out)
        assert Team.objects.count() == 50
        assert "50 already existed" in out.getvalue()

    def test_team_number_sequence(self):
        """Teams should be numbered 1-50."""
        call_command("init_teams", stdout=StringIO())

        team_numbers = list(Team.objects.values_list("team_number", flat=True).order_by("team_number"))
        assert team_numbers == list(range(1, 51))

    def test_team_name_format(self):
        """Teams should have correct name format."""
        call_command("init_teams", stdout=StringIO())

        team1 = Team.objects.get(team_number=1)
        team10 = Team.objects.get(team_number=10)

        assert team1.team_name == "BlueTeam01"
        assert team10.team_name == "BlueTeam10"

    def test_authentik_group_format(self):
        """Teams should have correct Authentik group format."""
        call_command("init_teams", stdout=StringIO())

        team1 = Team.objects.get(team_number=1)
        team50 = Team.objects.get(team_number=50)

        assert team1.authentik_group == "WCComps_BlueTeam01"
        assert team50.authentik_group == "WCComps_BlueTeam50"

    def test_teams_are_active(self):
        """Created teams should be active."""
        call_command("init_teams", stdout=StringIO())

        inactive_count = Team.objects.filter(is_active=False).count()
        assert inactive_count == 0


class TestWipeCompetitionData:
    """wipe_competition_data, behind Close Out Event and Clear leftover data."""

    @pytest.fixture
    def populated_database(self):
        """Create test data in the database."""
        team = Team.objects.create(team_number=1, team_name="Test Team", ticket_counter=34)
        ticket = Ticket.objects.create(
            ticket_number="T001-001",
            team=team,
            category=TicketCategory.objects.get(pk=6),
            title="Test Ticket",
            status="open",
        )
        TicketHistory.objects.create(ticket=ticket, action="created")
        test_user = User.objects.create_user(username="auth_user")
        DiscordLink.objects.create(
            discord_id=123456789,
            discord_username="testuser",
            user=test_user,
            team=team,
            is_active=True,
        )
        LinkAttempt.objects.create(
            discord_id=123456789,
            discord_username="testuser",
            authentik_username="auth_user",
            success=True,
        )
        LinkToken.objects.create(
            token="test_token",
            discord_id=123456789,
            discord_username="testuser",
            expires_at="2099-01-01T00:00:00Z",
        )
        DiscordTask.objects.create(task_type="log_to_channel", payload={"message": "hi"}, status="pending")
        return team

    def test_deletes_all_data_with_confirm(self, populated_database):
        """Competition data is deleted; static config is preserved."""
        assert Team.objects.count() == 1
        assert Ticket.objects.count() == 1
        assert DiscordLink.objects.count() == 1

        wipe_competition_data()

        # Competition data should be deleted
        assert Ticket.objects.count() == 0
        assert TicketHistory.objects.count() == 0
        assert DiscordLink.objects.count() == 0  # Blue team links deleted
        # Numbering restarts with the tickets: the next event's first ticket is T001-001, not T001-035
        assert Team.objects.get().ticket_counter == 0

        # Static config preserved (as documented in wipe_competition_data)
        assert Team.objects.count() == 1  # Teams are static config
        assert LinkAttempt.objects.count() == 1  # Harmless history
        assert LinkToken.objects.count() == 1  # Harmless
        assert DiscordTask.objects.count() == 1  # Task history

    def test_clears_orange_assignments_so_readiness_passes(self, populated_database, gold_team_user):
        from orange_team.models import OrangeAssignment, OrangeCheck, OrangeCheckIn

        from core.admin_views.readiness import ALL_CHECKS

        check = OrangeCheck.objects.create(title="Phones", description="", created_by=gold_team_user)
        OrangeAssignment.objects.create(orange_check=check, user=gold_team_user, team=populated_database)
        OrangeCheckIn.objects.create(user=gold_team_user)

        wipe_competition_data()

        assert dict(ALL_CHECKS)["No existing orange assignments"]()[0] == "pass"
        assert not OrangeCheckIn.objects.exists()
        assert OrangeCheck.objects.filter(pk=check.pk).exists()  # rubrics are reusable setup

    def test_reports_the_ticket_counter_reset_apart_from_the_deletions(self, populated_database):
        wiped = wipe_competition_data()

        assert wiped.deleted["Ticket"] == 1
        assert "TeamTicketCounter" not in wiped.deleted
        assert wiped.ticket_counters_reset == 1

    def test_handles_empty_database(self):
        wiped = wipe_competition_data()

        assert wiped.deleted["Ticket"] == 0
        assert wiped.ticket_counters_reset == 0
