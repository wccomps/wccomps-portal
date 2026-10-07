"""
Tests for scoring system - validates weighted scoring formula.

FORMULA DOCUMENTATION:
======================

Weighted Scoring (modifier derived from weights + raw maxes):
    total_pool = max(raw_max / (weight/100) for each category)
    modifier = (weight/100) × total_pool / raw_max
    total = (service × svc_mod) + (inject × inj_mod) + (orange × ora_mod)
          + sla + point_adjustments + red_deductions + incident_recovery

Default Weights / Raw Maxes:
    service: 40% / 11454     inject: 40% / 3060     orange: 20% / 160
"""

from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse

from core.models import UserGroups
from scoring.calculator import (
    calculate_suggested_recovery_points,
    calculate_team_score,
    compute_standings,
    suggest_red_score_matches,
)
from scoring.models import (
    IncidentReport,
    InjectScore,
    OrangeTeamScore,
    RedTeamScore,
    ScoringTemplate,
    ServiceScore,
)
from team.models import SchoolInfo, Team


class ScoringFormulaTests(TestCase):
    """Test weighted scoring formula."""

    def setUp(self) -> None:
        """Set up test data."""
        self.user = User.objects.create_user(username="testuser", password="test123")
        self.team1 = Team.objects.create(team_number=1, team_name="Test Team 1")
        self.team2 = Team.objects.create(team_number=2, team_name="Test Team 2")

        # Weights proportional to maxes → all modifiers = 1.0 for easy math
        # 1000/1600=62.5%, 500/1600=31.25%, 100/1600=6.25%
        self.template = ScoringTemplate.objects.create(
            service_weight=Decimal("62.50"),
            inject_weight=Decimal("31.25"),
            orange_weight=Decimal("6.25"),
            service_max=Decimal("1000"),
            inject_max=Decimal("500"),
            orange_max=Decimal("100"),
        )

    def test_simple_score_calculation(self) -> None:
        """Test basic scaling-factor score calculation."""
        # Service: 500 × 1.0 = 500, SLA: -10
        ServiceScore.objects.create(
            team=self.team1,
            service_points=Decimal("500.00"),
            sla_violations=Decimal("-10.00"),
        )

        # Inject: 250 × 1.0 = 250
        InjectScore.objects.create(
            team=self.team1,
            inject_id="INJ-001",
            inject_name="Test Inject",
            max_points=Decimal("100.00"),
            points_awarded=Decimal("250.00"),
            graded_by=self.user,
            is_approved=True,
        )

        # Orange: 50 × 1.0 = 50
        OrangeTeamScore.objects.create(
            team=self.team1,
            description="Security improvement",
            points_awarded=Decimal("50.00"),
            submitted_by=self.user,
            is_approved=True,
        )

        # Red: -100 (subtracted directly)
        red_finding = RedTeamScore.objects.create(
            attack_vector="Test attack",
            source_ip="10.0.0.5",
            points_per_team=Decimal("100.00"),
            submitted_by=self.user,
            is_approved=True,
        )
        red_finding.affected_teams.add(self.team1)

        scores = calculate_team_score(self.team1)

        # Service: 500 × 1.0 = 500
        self.assertEqual(scores["service_points"], Decimal("500.0"))
        # Inject: 250 × 1.0 = 250
        self.assertEqual(scores["inject_points"], Decimal("250.0"))
        # Orange: 50 × 1.0 = 50
        self.assertEqual(scores["orange_points"], Decimal("50.0"))
        # Red deduction: -100 (raw)
        self.assertEqual(scores["red_deductions"], Decimal("-100"))
        # SLA penalty
        self.assertEqual(scores["sla_penalties"], Decimal("-10.00"))
        # Recovery points
        self.assertEqual(scores["incident_recovery_points"], Decimal("0"))
        # Total = 500 + 250 + 50 - 10 - 100 = 690
        self.assertEqual(scores["total_score"], Decimal("690.0"))

    def test_zero_scores_all_categories(self) -> None:
        """Team with no scoring data gets total 0."""
        scores = calculate_team_score(self.team1)

        self.assertEqual(scores["service_points"], Decimal("0"))
        self.assertEqual(scores["inject_points"], Decimal("0"))
        self.assertEqual(scores["orange_points"], Decimal("0"))
        self.assertEqual(scores["red_deductions"], Decimal("0"))
        self.assertEqual(scores["total_score"], Decimal("0"))

    def test_only_red_deductions(self) -> None:
        """Team with only red findings gets negative total."""
        red = RedTeamScore.objects.create(
            attack_vector="RCE",
            source_ip="10.0.0.1",
            points_per_team=Decimal("200.00"),
            submitted_by=self.user,
            is_approved=True,
        )
        red.affected_teams.add(self.team1)

        scores = calculate_team_score(self.team1)

        self.assertEqual(scores["red_deductions"], Decimal("-200"))
        self.assertLess(scores["total_score"], Decimal("0"))

    def test_unapproved_scores_excluded(self) -> None:
        """Unapproved inject grades, red findings, and orange team checks are not counted."""
        InjectScore.objects.create(
            team=self.team1,
            inject_id="INJ-X",
            inject_name="Unapproved Inject",
            max_points=Decimal("100.00"),
            points_awarded=Decimal("80.00"),
            graded_by=self.user,
            is_approved=False,
        )
        OrangeTeamScore.objects.create(
            team=self.team1,
            description="Unapproved bonus",
            points_awarded=Decimal("30.00"),
            submitted_by=self.user,
            is_approved=False,
        )
        red = RedTeamScore.objects.create(
            attack_vector="Unapproved finding",
            source_ip="10.0.0.2",
            points_per_team=Decimal("50.00"),
            submitted_by=self.user,
            is_approved=False,
        )
        red.affected_teams.add(self.team1)

        scores = calculate_team_score(self.team1)

        self.assertEqual(scores["inject_points"], Decimal("0"))
        self.assertEqual(scores["orange_points"], Decimal("0"))
        self.assertEqual(scores["red_deductions"], Decimal("0"))
        self.assertEqual(scores["total_score"], Decimal("0"))

    def test_leaderboard_ranking(self) -> None:
        """Test that leaderboard ranks teams correctly."""
        ServiceScore.objects.create(
            team=self.team1,
            service_points=Decimal("500.00"),
        )
        ServiceScore.objects.create(
            team=self.team2,
            service_points=Decimal("300.00"),
        )

        standings = {standing.team: standing for standing in compute_standings()}
        team1_score = standings[self.team1]
        team2_score = standings[self.team2]

        self.assertEqual(team1_score.rank, 1)
        self.assertEqual(team2_score.rank, 2)
        self.assertGreater(team1_score.total_score, team2_score.total_score)


pytestmark = pytest.mark.django_db


@pytest.mark.django_db
class TestLeaderboardAccess:
    """Test leaderboard view access restrictions."""

    def test_unauthenticated_user_denied_access(self) -> None:
        """Unauthenticated users should be redirected to login."""
        client = Client()
        response = client.get(reverse("leaderboard_page"))

        assert response.status_code == 302
        assert "login" in response.url

    def test_gold_team_can_access_leaderboard(self, create_user_with_groups) -> None:
        """Gold Team members should be able to access the leaderboard."""
        user = create_user_with_groups("gold_user", ["WCComps_GoldTeam"])
        client = Client()
        client.force_login(user)

        response = client.get(reverse("leaderboard_page"))

        assert response.status_code == 200

    def test_white_team_can_access_leaderboard(self, create_user_with_groups) -> None:
        """White Team members should be able to access the leaderboard."""
        user = create_user_with_groups("white_user", ["WCComps_WhiteTeam"])
        client = Client()
        client.force_login(user)

        response = client.get(reverse("leaderboard_page"))

        assert response.status_code == 200

    def test_ticketing_admin_can_access_leaderboard(self, create_user_with_groups) -> None:
        """Ticketing Admin should be able to access the leaderboard."""
        user = create_user_with_groups("ticketing_admin", ["WCComps_Ticketing_Admin"])
        client = Client()
        client.force_login(user)

        assert client.get(reverse("leaderboard_page")).status_code == 200

    def test_admin_can_access_leaderboard(self) -> None:
        """System Admin (Gold Team) should be able to access the leaderboard."""
        user = User.objects.create_user(username="admin_user", password="test123")
        UserGroups.objects.create(user=user, authentik_id="admin_user-uid", groups=["WCComps_GoldTeam"])
        client = Client()
        client.force_login(user)

        response = client.get(reverse("leaderboard_page"))

        assert response.status_code == 200

    def test_blue_team_denied_access(self, create_user_with_groups) -> None:
        """Blue Team members should be denied access to the leaderboard."""
        user = create_user_with_groups("blue_user", ["WCComps_BlueTeam01"])
        client = Client()
        client.force_login(user)

        response = client.get(reverse("leaderboard_page"))

        assert response.status_code == 302

    def test_red_team_allowed_access(self, create_user_with_groups) -> None:
        """Red Team members should have access to the leaderboard."""
        user = create_user_with_groups("red_user", ["WCComps_RedTeam"])
        client = Client()
        client.force_login(user)

        response = client.get(reverse("leaderboard_page"))

        assert response.status_code == 200

    def test_orange_team_can_access_leaderboard(self, create_user_with_groups) -> None:
        """Orange Team members should have access to the leaderboard."""
        user = create_user_with_groups("orange_user", ["WCComps_OrangeTeam"])
        client = Client()
        client.force_login(user)

        response = client.get(reverse("leaderboard_page"))

        assert response.status_code == 200

    def test_ticketing_support_can_access_leaderboard(self, create_user_with_groups) -> None:
        """Ticketing Support members should have access to the leaderboard."""
        user = create_user_with_groups("ticketing_support", ["WCComps_Ticketing_Support"])
        client = Client()
        client.force_login(user)

        response = client.get(reverse("leaderboard_page"))

        assert response.status_code == 200

    def test_user_with_no_groups_denied_access(self, create_user_with_groups) -> None:
        """Users with no groups should be denied access to the leaderboard."""
        user = create_user_with_groups("no_group_user", [])
        client = Client()
        client.force_login(user)

        response = client.get(reverse("leaderboard_page"))

        assert response.status_code == 302

    def test_user_with_multiple_allowed_groups_can_access(self, create_user_with_groups) -> None:
        """Users with multiple groups including an allowed one should be able to access."""
        user = create_user_with_groups("multi_group_user", ["WCComps_GoldTeam", "WCComps_BlueTeam01"])
        client = Client()
        client.force_login(user)

        response = client.get(reverse("leaderboard_page"))

        assert response.status_code == 200

    def test_leaderboard_has_single_header(self, create_user_with_groups) -> None:
        """The leaderboard page should only have one header that says Leaderboard."""
        user = create_user_with_groups("gold_user", ["WCComps_GoldTeam"])
        client = Client()
        client.force_login(user)

        response = client.get(reverse("leaderboard_page"))
        assert response.status_code == 200
        content = response.content.decode()
        assert "<h2>Leaderboard</h2>" not in content
        assert "breadcrumbs" in content
        assert "Leaderboard" in content

    def test_discord_admin_can_toggle_school_names(self, create_user_with_groups) -> None:
        """Discord admin sees the school names toggle button and school name markup."""
        team = Team.objects.create(team_number=1, team_name="Team 1")
        SchoolInfo.objects.create(team=team, school_name="Test University", contact_email="test@example.edu")
        ServiceScore.objects.create(team=team, service_points=100)

        admin_user = create_user_with_groups("admin_user", ["WCComps_Discord_Admin"])
        client = Client()
        client.force_login(admin_user)

        response = client.get(reverse("leaderboard_page"))
        assert response.status_code == 200
        content = response.content.decode()
        assert "toggle-names-btn" in content
        assert "Show School Names" in content
        assert "Show Player Names" in content
        assert "Test University" in content
        assert "Team 1" in content

    def test_discord_admin_schools_query_param(self, create_user_with_groups) -> None:
        """Discord admin with ?schools=1 loads with show_schools active."""
        admin_user = create_user_with_groups("admin_user", ["WCComps_Discord_Admin"])
        client = Client()
        client.force_login(admin_user)

        response = client.get(reverse("leaderboard_page") + "?schools=1")
        assert response.status_code == 200
        assert response.context["show_schools"] is True

    def test_non_admin_cannot_toggle_school_names(self, create_user_with_groups) -> None:
        """Non-admin roles should not see the toggle button or school names."""
        team = Team.objects.create(team_number=1, team_name="Team 1")
        SchoolInfo.objects.create(team=team, school_name="Test University", contact_email="test@example.edu")
        ServiceScore.objects.create(team=team, service_points=100)

        gold_user = create_user_with_groups("gold_user", ["WCComps_GoldTeam"])
        client = Client()
        client.force_login(gold_user)

        response = client.get(reverse("leaderboard_page") + "?schools=1")
        assert response.status_code == 200
        assert response.context.get("show_schools", False) is False
        content = response.content.decode()
        assert "toggle-names-btn" not in content
        assert "Show School Names" not in content
        assert "Test University" not in content

    def test_leaderboard_orange_points_plain_white(self, create_user_with_groups) -> None:
        """Orange team points should render as plain text, not green with +."""
        from scoring.models import OrangeTeamScore, ScoringTemplate

        ScoringTemplate.objects.create(service_max=Decimal("40"), inject_max=Decimal("40"), orange_max=Decimal("20"))
        team = Team.objects.create(team_number=1, team_name="Team 1")
        OrangeTeamScore.objects.create(team=team, points_awarded=Decimal("25"), description="test", is_approved=True)

        admin_user = create_user_with_groups("admin_user", ["WCComps_Discord_Admin"])
        client = Client()
        client.force_login(admin_user)

        response = client.get(reverse("leaderboard_page"))
        assert response.status_code == 200
        content = response.content.decode()
        # Should render 25 as plain text in the table cell, not wrapped in score-positive or signed +
        assert '<td class="text-right">25</td>' in content
        assert "+25" not in content

    def test_leaderboard_buttons_primary_styling(self, create_user_with_groups) -> None:
        """Buttons at the bottom of leaderboard should use primary button styling."""
        admin_user = create_user_with_groups("admin_user", ["WCComps_Discord_Admin"])
        client = Client()
        client.force_login(admin_user)

        response = client.get(reverse("leaderboard_page"))
        assert response.status_code == 200
        content = response.content.decode()
        assert "Download All PDFs" in content
        assert "JSON" in content
        assert "Email All Scorecards" in content
        assert "Sync Service Scores" in content
        # Links and buttons should have the 'button default' class
        assert 'class="button default "' in content
        assert 'class="button  default "' in content or 'class="button default "' in content
        # Action links styled as buttons should have role="button" so Django admin doesn't underline them
        assert 'role="button"' in content


class InjectScoreApprovalTests(TestCase):
    """Test approval tracking fields on InjectScore model."""

    def setUp(self) -> None:
        """Set up test data."""
        self.grader = User.objects.create_user(username="grader", password="test123")
        self.approver = User.objects.create_user(username="approver", password="test123")
        self.team = Team.objects.create(team_number=1, team_name="Test Team")

    def test_new_inject_grade_defaults_to_not_approved(self) -> None:
        """New InjectScore should have is_approved=False by default."""
        grade = InjectScore.objects.create(
            team=self.team,
            inject_id="INJ-001",
            inject_name="Test Inject",
            max_points=Decimal("100.00"),
            points_awarded=Decimal("80.00"),
            graded_by=self.grader,
        )

        self.assertFalse(grade.is_approved)
        self.assertIsNone(grade.approved_at)
        self.assertIsNone(grade.approved_by)

    def test_inject_grade_can_be_approved(self) -> None:
        """InjectScore can be marked as approved with timestamp and user."""
        from django.utils import timezone

        grade = InjectScore.objects.create(
            team=self.team,
            inject_id="INJ-002",
            inject_name="Another Inject",
            max_points=Decimal("50.00"),
            points_awarded=Decimal("45.00"),
            graded_by=self.grader,
        )

        approval_time = timezone.now()
        grade.is_approved = True
        grade.approved_at = approval_time
        grade.approved_by = self.approver
        grade.save()

        grade.refresh_from_db()
        self.assertTrue(grade.is_approved)
        self.assertEqual(grade.approved_at, approval_time)
        self.assertEqual(grade.approved_by, self.approver)

    def test_can_query_unapproved_grades(self) -> None:
        """Can query for unapproved InjectScore records."""
        InjectScore.objects.create(
            team=self.team,
            inject_id="INJ-003",
            inject_name="Unapproved Inject",
            max_points=Decimal("100.00"),
            points_awarded=Decimal("90.00"),
            graded_by=self.grader,
        )

        approved_grade = InjectScore.objects.create(
            team=self.team,
            inject_id="INJ-004",
            inject_name="Approved Inject",
            max_points=Decimal("100.00"),
            points_awarded=Decimal("95.00"),
            graded_by=self.grader,
        )
        from django.utils import timezone

        approved_grade.is_approved = True
        approved_grade.approved_at = timezone.now()
        approved_grade.approved_by = self.approver
        approved_grade.save()

        unapproved_grades = InjectScore.objects.filter(is_approved=False)
        self.assertEqual(unapproved_grades.count(), 1)
        self.assertEqual(unapproved_grades.first().inject_id, "INJ-003")

    def test_can_query_approved_grades(self) -> None:
        """Can query for approved InjectScore records."""
        from django.utils import timezone

        grade1 = InjectScore.objects.create(
            team=self.team,
            inject_id="INJ-005",
            inject_name="First Approved",
            max_points=Decimal("100.00"),
            points_awarded=Decimal("85.00"),
            graded_by=self.grader,
        )
        grade1.is_approved = True
        grade1.approved_at = timezone.now()
        grade1.approved_by = self.approver
        grade1.save()

        InjectScore.objects.create(
            team=self.team,
            inject_id="INJ-006",
            inject_name="Not Approved",
            max_points=Decimal("100.00"),
            points_awarded=Decimal("70.00"),
            graded_by=self.grader,
        )

        approved_grades = InjectScore.objects.filter(is_approved=True)
        self.assertEqual(approved_grades.count(), 1)
        self.assertEqual(approved_grades.first().inject_id, "INJ-005")

    def test_approved_by_can_be_null(self) -> None:
        """approved_by field can be null (for system-approved or legacy records)."""
        from django.utils import timezone

        grade = InjectScore.objects.create(
            team=self.team,
            inject_id="INJ-007",
            inject_name="System Approved",
            max_points=Decimal("100.00"),
            points_awarded=Decimal("100.00"),
            graded_by=self.grader,
        )

        grade.is_approved = True
        grade.approved_at = timezone.now()
        grade.approved_by = None
        grade.save()

        grade.refresh_from_db()
        self.assertTrue(grade.is_approved)
        self.assertIsNotNone(grade.approved_at)
        self.assertIsNone(grade.approved_by)

    def test_approved_by_set_null_on_user_delete(self) -> None:
        """When approver user is deleted, approved_by should be set to NULL."""
        from django.utils import timezone

        grade = InjectScore.objects.create(
            team=self.team,
            inject_id="INJ-008",
            inject_name="Test Delete",
            max_points=Decimal("100.00"),
            points_awarded=Decimal("88.00"),
            graded_by=self.grader,
        )

        grade.is_approved = True
        grade.approved_at = timezone.now()
        grade.approved_by = self.approver
        grade.save()

        grade.refresh_from_db()
        self.assertEqual(grade.approved_by, self.approver)

        self.approver.delete()

        grade.refresh_from_db()
        self.assertTrue(grade.is_approved)
        self.assertIsNone(grade.approved_by)


@pytest.mark.django_db
class TestIncidentFindingMatching:
    """Test incident-to-finding matching workflow."""

    def test_incident_can_be_matched_to_finding(self, create_user_with_groups) -> None:
        """Incident can be matched to a red team finding."""
        user = create_user_with_groups("gold_user", ["WCComps_GoldTeam"])
        team = Team.objects.create(team_number=1, team_name="Test Team")

        red_finding = RedTeamScore.objects.create(
            attack_vector="SQL Injection",
            source_ip="10.0.0.5",
            points_per_team=Decimal("50.00"),
            submitted_by=user,
        )
        red_finding.affected_teams.add(team)

        incident = IncidentReport.objects.create(
            team=team,
            submitted_by=user,
            attack_description="Detected SQL injection attempt",
            source_ip="10.0.0.5",
            destination_ip="10.100.11.22",
            attack_detected_at="2025-01-01T12:00:00Z",
        )

        assert incident.matched_to_red_score is None
        assert incident.is_approved is False
        assert incident.approved_by is None
        assert incident.approved_at is None

        incident.matched_to_red_score = red_finding
        incident.is_approved = True
        incident.approved_by = user
        incident.approved_at = "2025-01-01T13:00:00Z"
        incident.points_returned = Decimal("40.00")
        incident.save()

        incident.refresh_from_db()
        assert incident.matched_to_red_score == red_finding
        assert incident.is_approved is True
        assert incident.approved_by == user
        assert incident.approved_at is not None
        assert incident.points_returned == Decimal("40.00")

    def test_incident_can_be_rejected_without_match(self, create_user_with_groups) -> None:
        """Incident can be rejected (reviewed but not matched to finding)."""
        user = create_user_with_groups("gold_user", ["WCComps_GoldTeam"])
        team = Team.objects.create(team_number=1, team_name="Test Team")

        incident = IncidentReport.objects.create(
            team=team,
            submitted_by=user,
            attack_description="False positive - normal traffic",
            source_ip="10.0.0.5",
            destination_ip="10.100.11.22",
            attack_detected_at="2025-01-01T12:00:00Z",
        )

        incident.matched_to_red_score = None
        incident.is_approved = True
        incident.approved_by = user
        incident.approved_at = "2025-01-01T13:00:00Z"
        incident.points_returned = Decimal("0.00")
        incident.approval_notes = "False positive - normal scanning traffic"
        incident.save()

        incident.refresh_from_db()
        assert incident.matched_to_red_score is None
        assert incident.is_approved is True
        assert incident.approved_by == user
        assert incident.approved_at is not None
        assert incident.points_returned == Decimal("0.00")

    def test_only_reviewed_incidents_count_for_scoring(self, create_user_with_groups) -> None:
        """Only reviewed incidents contribute to incident recovery points."""
        user = create_user_with_groups("gold_user", ["WCComps_GoldTeam"])
        team = Team.objects.create(team_number=1, team_name="Test Team")

        # Create template with equal weights (modifier=1.0 when weight ∝ max)
        ScoringTemplate.objects.create(
            service_weight=Decimal("62.50"),
            inject_weight=Decimal("31.25"),
            orange_weight=Decimal("6.25"),
            service_max=Decimal("1000"),
            inject_max=Decimal("500"),
            orange_max=Decimal("100"),
        )

        IncidentReport.objects.create(
            team=team,
            submitted_by=user,
            attack_description="Detected attack",
            source_ip="10.0.0.5",
            destination_ip="10.100.11.22",
            attack_detected_at="2025-01-01T12:00:00Z",
            is_approved=True,
            approved_by=user,
            approved_at="2025-01-01T13:00:00Z",
            points_returned=Decimal("40.00"),
        )

        IncidentReport.objects.create(
            team=team,
            submitted_by=user,
            attack_description="Another attack",
            source_ip="10.0.0.6",
            destination_ip="10.100.11.23",
            attack_detected_at="2025-01-01T14:00:00Z",
            points_returned=Decimal("30.00"),
        )

        scores = calculate_team_score(team)

        # Only reviewed incident counts - recovery is raw value for display
        # (40 points returned from reviewed incident)
        assert scores["incident_recovery_points"] == Decimal("40.00")

    def test_suggest_red_score_matches_by_source_ip(self, create_user_with_groups) -> None:
        """Finding suggestion algorithm matches by source IP."""
        user = create_user_with_groups("gold_user", ["WCComps_GoldTeam"])
        team = Team.objects.create(team_number=1, team_name="Test Team")

        matching_finding = RedTeamScore.objects.create(
            attack_vector="RCE",
            source_ip="10.0.0.5",
            points_per_team=Decimal("50.00"),
            submitted_by=user,
        )
        matching_finding.affected_teams.add(team)

        non_matching_finding = RedTeamScore.objects.create(
            attack_vector="SQLi",
            source_ip="10.0.0.99",
            points_per_team=Decimal("30.00"),
            submitted_by=user,
        )
        non_matching_finding.affected_teams.add(team)

        incident = IncidentReport.objects.create(
            team=team,
            submitted_by=user,
            attack_description="Detected attack",
            source_ip="10.0.0.5",
            destination_ip="10.100.11.22",
            attack_detected_at="2025-01-01T12:00:00Z",
        )

        suggestions = suggest_red_score_matches(incident)

        assert matching_finding in suggestions
        assert len(suggestions) >= 1

    def test_suggest_red_score_matches_by_box_and_service(self, create_user_with_groups) -> None:
        """Finding suggestion algorithm matches by affected box and service."""
        user = create_user_with_groups("gold_user", ["WCComps_GoldTeam"])
        team = Team.objects.create(team_number=1, team_name="Test Team")

        matching_finding = RedTeamScore.objects.create(
            attack_vector="Web Exploit",
            source_ip="10.0.0.5",
            affected_boxes=["web-server"],
            affected_service="HTTP",
            points_per_team=Decimal("50.00"),
            submitted_by=user,
        )
        matching_finding.affected_teams.add(team)

        incident = IncidentReport.objects.create(
            team=team,
            submitted_by=user,
            attack_description="Web server compromised",
            source_ip="10.0.0.7",
            destination_ip="10.100.11.22",
            affected_boxes=["web-server"],
            affected_service="HTTP",
            attack_detected_at="2025-01-01T12:00:00Z",
        )

        suggestions = suggest_red_score_matches(incident)

        assert matching_finding in suggestions

    def test_calculate_suggested_recovery_points(self, create_user_with_groups) -> None:
        """Suggested recovery points calculated at 80% of red team deduction."""
        user = create_user_with_groups("gold_user", ["WCComps_GoldTeam"])
        team = Team.objects.create(team_number=1, team_name="Test Team")

        red_finding = RedTeamScore.objects.create(
            attack_vector="RCE",
            source_ip="10.0.0.5",
            points_per_team=Decimal("50.00"),
            submitted_by=user,
        )

        incident = IncidentReport.objects.create(
            team=team,
            submitted_by=user,
            attack_description="Detected attack",
            source_ip="10.0.0.5",
            destination_ip="10.100.11.22",
            attack_detected_at="2025-01-01T12:00:00Z",
        )

        suggested_points = calculate_suggested_recovery_points(incident, red_finding)

        assert suggested_points == Decimal("40.00")

    def test_reviewer_tracking_fields_nullable(self, create_user_with_groups) -> None:
        """Reviewer tracking fields can be null for unreviewed incidents."""
        user = create_user_with_groups("blue_user", ["WCComps_BlueTeam01"])
        team = Team.objects.create(team_number=1, team_name="Test Team")

        incident = IncidentReport.objects.create(
            team=team,
            submitted_by=user,
            attack_description="Pending review",
            source_ip="10.0.0.5",
            destination_ip="10.100.11.22",
            attack_detected_at="2025-01-01T12:00:00Z",
        )

        assert incident.approved_by is None
        assert incident.approved_at is None
        assert incident.is_approved is False


class ScalingContextTests(TestCase):
    """Test that calculator exposes raw scores and modifiers."""

    def setUp(self) -> None:
        self.user = User.objects.create_user(username="testuser", password="test123")
        self.team = Team.objects.create(team_number=1, team_name="Test Team")
        ScoringTemplate.objects.create(
            service_weight=Decimal("40"),
            inject_weight=Decimal("40"),
            orange_weight=Decimal("20"),
            service_max=Decimal("11454"),
            inject_max=Decimal("3060"),
            orange_max=Decimal("160"),
        )
        ServiceScore.objects.create(
            team=self.team,
            service_points=Decimal("8000"),
            sla_violations=Decimal("0"),
            point_adjustments=Decimal("0"),
        )
        InjectScore.objects.create(
            team=self.team,
            inject_id="inj-1",
            inject_name="Inject 1",
            points_awarded=Decimal("2000"),
            is_approved=True,
        )

    def test_calculate_team_score_detailed_returns_raw_and_modifiers(self) -> None:
        from scoring.calculator import calculate_team_score_detailed

        result = calculate_team_score_detailed(self.team)

        assert "service_raw" in result
        assert "inject_raw" in result
        assert "orange_raw" in result
        assert "service_modifier" in result
        assert "inject_modifier" in result
        assert "orange_modifier" in result
        assert "service_weight" in result
        assert "inject_weight" in result
        assert "orange_weight" in result
        assert result["service_raw"] == Decimal("8000")
        assert result["inject_raw"] == Decimal("2000")


@pytest.mark.django_db
class TestIncidentListView:
    """Test blue team incident list view."""

    def test_unauthenticated_user_redirected_to_login(self) -> None:
        """Unauthenticated users redirected to login when accessing incident list."""
        client = Client()
        response = client.get(reverse("scoring:incident_list"))

        assert response.status_code == 302
        assert "login" in response.url

    def test_blue_team_can_access_incident_list(self, create_user_with_groups) -> None:
        """Blue team members can access their incident list."""
        user = create_user_with_groups("blue_user", ["WCComps_BlueTeam01"])
        Team.objects.create(team_number=1, team_name="Team 01")
        client = Client()
        client.force_login(user)

        response = client.get(reverse("scoring:incident_list"))

        assert response.status_code == 200

    def test_non_blue_team_user_denied_access(self, create_user_with_groups) -> None:
        """Non-blue team users denied access to incident list."""
        user = create_user_with_groups("red_user", ["WCComps_RedTeam"])
        client = Client()
        client.force_login(user)

        response = client.get(reverse("scoring:incident_list"))

        assert response.status_code == 403

    def test_user_without_team_denied_access(self, create_user_with_groups) -> None:
        """Users without a team assignment denied access."""
        user = create_user_with_groups("no_team_user", [])
        client = Client()
        client.force_login(user)

        response = client.get(reverse("scoring:incident_list"))

        assert response.status_code == 403

    def test_admin_can_access_incident_list(self) -> None:
        """Admin users can access incident list."""
        user = User.objects.create_user(username="admin_user", password="test123")
        UserGroups.objects.create(user=user, authentik_id="admin_user-uid", groups=["WCComps_GoldTeam"])
        client = Client()
        client.force_login(user)

        response = client.get(reverse("scoring:incident_list"))

        assert response.status_code == 200

    def test_white_team_can_access_incident_list(self) -> None:
        """White team members can access incident list (sees all incidents)."""
        user = User.objects.create_user(username="white_user", password="test123")
        UserGroups.objects.create(user=user, authentik_id="white_user-uid", groups=["WCComps_WhiteTeam"])
        client = Client()
        client.force_login(user)

        response = client.get(reverse("scoring:incident_list"))

        assert response.status_code == 200

    def test_blue_team_sees_only_their_incidents(self, create_user_with_groups) -> None:
        """Blue team members see only their team's incidents."""
        user1 = create_user_with_groups("blue_user1", ["WCComps_BlueTeam01"])
        user2 = create_user_with_groups("blue_user2", ["WCComps_BlueTeam02"])
        team1 = Team.objects.create(team_number=1, team_name="Team 01")
        team2 = Team.objects.create(team_number=2, team_name="Team 02")

        incident1 = IncidentReport.objects.create(
            team=team1,
            submitted_by=user1,
            attack_description="Team 1 incident",
            source_ip="10.0.0.1",
            attack_detected_at="2025-01-01T12:00:00Z",
        )
        incident2 = IncidentReport.objects.create(
            team=team2,
            submitted_by=user2,
            attack_description="Team 2 incident",
            source_ip="10.0.0.2",
            attack_detected_at="2025-01-01T13:00:00Z",
        )

        client = Client()
        client.force_login(user1)
        response = client.get(reverse("scoring:incident_list"))

        assert response.status_code == 200
        assert incident1 in response.context["incidents"]
        assert incident2 not in response.context["incidents"]
        assert len(response.context["incidents"]) == 1

    def test_incident_list_shows_correct_fields(self, create_user_with_groups) -> None:
        """Incident list shows timestamp, box, and attack type."""
        user = create_user_with_groups("blue_user", ["WCComps_BlueTeam01"])
        team = Team.objects.create(team_number=1, team_name="Team 01")

        IncidentReport.objects.create(
            team=team,
            submitted_by=user,
            attack_description="SQL Injection detected",
            source_ip="10.0.0.5",
            affected_boxes=["web-server"],
            attack_detected_at="2025-01-01T12:00:00Z",
        )

        client = Client()
        client.force_login(user)
        response = client.get(reverse("scoring:incident_list"))

        assert response.status_code == 200
        content = response.content.decode()
        assert "web-server" in content
        assert "SQL Injection detected" in content

    def test_incident_list_does_not_show_match_status(self, create_user_with_groups) -> None:
        """Incident list does NOT show match status to blue team."""
        user = create_user_with_groups("blue_user", ["WCComps_BlueTeam01"])
        gold_user = create_user_with_groups("gold_user", ["WCComps_GoldTeam"])
        team = Team.objects.create(team_number=1, team_name="Team 01")

        red_finding = RedTeamScore.objects.create(
            attack_vector="Test attack",
            source_ip="10.0.0.5",
            points_per_team=Decimal("30.00"),
            submitted_by=gold_user,
        )

        IncidentReport.objects.create(
            team=team,
            submitted_by=user,
            attack_description="Detected attack",
            source_ip="10.0.0.5",
            attack_detected_at="2025-01-01T12:00:00Z",
            is_approved=True,
            matched_to_red_score=red_finding,
            points_returned=Decimal("24.00"),
            approved_by=gold_user,
        )

        client = Client()
        client.force_login(user)
        response = client.get(reverse("scoring:incident_list"))

        assert response.status_code == 200
        content = response.content.decode()
        assert "24.00" not in content
        assert "matched" not in content.lower() or "match" in "incident"

    def test_incident_list_shows_multiple_incidents_in_order(self, create_user_with_groups) -> None:
        """Incident list shows multiple incidents ordered by submission time."""
        user = create_user_with_groups("blue_user", ["WCComps_BlueTeam01"])
        team = Team.objects.create(team_number=1, team_name="Team 01")

        incident1 = IncidentReport.objects.create(
            team=team,
            submitted_by=user,
            attack_description="First incident",
            source_ip="10.0.0.1",
            attack_detected_at="2025-01-01T10:00:00Z",
        )
        incident2 = IncidentReport.objects.create(
            team=team,
            submitted_by=user,
            attack_description="Second incident",
            source_ip="10.0.0.2",
            attack_detected_at="2025-01-01T11:00:00Z",
        )
        incident3 = IncidentReport.objects.create(
            team=team,
            submitted_by=user,
            attack_description="Third incident",
            source_ip="10.0.0.3",
            attack_detected_at="2025-01-01T12:00:00Z",
        )

        client = Client()
        client.force_login(user)
        response = client.get(reverse("scoring:incident_list"))

        assert response.status_code == 200
        incidents = list(response.context["incidents"])
        assert len(incidents) == 3
        assert incidents[0] == incident3
        assert incidents[1] == incident2
        assert incidents[2] == incident1

    def test_incident_list_empty_when_no_incidents(self, create_user_with_groups) -> None:
        """Incident list shows empty state when team has no incidents."""
        user = create_user_with_groups("blue_user", ["WCComps_BlueTeam01"])
        Team.objects.create(team_number=1, team_name="Team 01")
        client = Client()
        client.force_login(user)

        response = client.get(reverse("scoring:incident_list"))

        assert response.status_code == 200
        assert len(response.context["incidents"]) == 0

    def test_incident_list_links_to_detail_view(self, create_user_with_groups) -> None:
        """Incident list contains links to detail view."""
        user = create_user_with_groups("blue_user", ["WCComps_BlueTeam01"])
        team = Team.objects.create(team_number=1, team_name="Team 01")

        incident = IncidentReport.objects.create(
            team=team,
            submitted_by=user,
            attack_description="Test incident",
            source_ip="10.0.0.5",
            attack_detected_at="2025-01-01T12:00:00Z",
        )

        client = Client()
        client.force_login(user)
        response = client.get(reverse("scoring:incident_list"))

        assert response.status_code == 200
        content = response.content.decode()
        expected_url = reverse("scoring:view_incident_report", kwargs={"incident_id": incident.id})
        assert expected_url in content
