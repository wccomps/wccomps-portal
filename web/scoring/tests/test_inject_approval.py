"""Tests for inject grade approval workflow (FEAT-5)."""

from collections.abc import Callable
from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from core.models import UserGroups
from scoring.models import InjectScore
from team.models import Team


@pytest.mark.django_db
class TestInjectScoresReviewAccess:
    """Test access control for inject grades review view."""

    def test_unauthenticated_user_denied_access(self, create_user_with_groups: Callable[..., User]) -> None:
        """Unauthenticated users should be redirected to login."""
        client = Client()
        response = client.get(reverse("scoring:inject_grades_review"))

        assert response.status_code == 302
        assert "/auth/" in response.url and "login" in response.url

    def test_gold_team_can_access_review(self, create_user_with_groups: Callable[..., User]) -> None:
        """Gold Team members should be able to access inject grades review."""
        user = create_user_with_groups("gold_user", ["WCComps_GoldTeam"])
        client = Client()
        client.force_login(user)

        response = client.get(reverse("scoring:inject_grades_review"))

        assert response.status_code == 200

    def test_admin_can_access_review(self, create_user_with_groups: Callable[..., User]) -> None:
        """System Admin (Gold Team) should be able to access inject grades review."""
        user = User.objects.create_user(username="admin_user", password="test123")
        UserGroups.objects.create(user=user, authentik_id="admin_user-uid", groups=["WCComps_GoldTeam"])
        client = Client()
        client.force_login(user)

        response = client.get(reverse("scoring:inject_grades_review"))

        assert response.status_code == 200

    def test_white_team_can_access_review(self, create_user_with_groups: Callable[..., User]) -> None:
        """White Team members should be able to access inject grades review."""
        user = create_user_with_groups("white_user", ["WCComps_WhiteTeam"])
        client = Client()
        client.force_login(user)

        response = client.get(reverse("scoring:inject_grades_review"))

        assert response.status_code == 200

    def test_red_team_denied_access(self, create_user_with_groups: Callable[..., User]) -> None:
        """Red Team members should be denied access."""
        user = create_user_with_groups("red_user", ["WCComps_RedTeam"])
        client = Client()
        client.force_login(user)

        response = client.get(reverse("scoring:inject_grades_review"))

        assert response.status_code == 403

    def test_blue_team_denied_access(self, create_user_with_groups: Callable[..., User]) -> None:
        """Blue Team members should be denied access."""
        user = create_user_with_groups("blue_user", ["WCComps_BlueTeam01"])
        client = Client()
        client.force_login(user)

        response = client.get(reverse("scoring:inject_grades_review"))

        assert response.status_code == 403


@pytest.mark.django_db
class TestInjectScoresGrouping:
    """Test inject grades are grouped correctly by inject."""

    def test_grades_displayed_in_page(self, create_user_with_groups: Callable[..., User]) -> None:
        """Grades should be displayed in page_obj context."""
        user = create_user_with_groups("gold_user", ["WCComps_GoldTeam"])
        grader = User.objects.create_user(username="grader", password="test123")

        team1 = Team.objects.create(team_number=1, team_name="Team 1")
        team2 = Team.objects.create(team_number=2, team_name="Team 2")

        # Create grades for two different injects
        InjectScore.objects.create(
            team=team1,
            inject_id="INJ-001",
            inject_name="Security Policy",
            max_points=Decimal("100.00"),
            points_awarded=Decimal("85.00"),
            graded_by=grader,
        )
        InjectScore.objects.create(
            team=team2,
            inject_id="INJ-001",
            inject_name="Security Policy",
            max_points=Decimal("100.00"),
            points_awarded=Decimal("90.00"),
            graded_by=grader,
        )
        InjectScore.objects.create(
            team=team1,
            inject_id="INJ-002",
            inject_name="Network Diagram",
            max_points=Decimal("50.00"),
            points_awarded=Decimal("45.00"),
            graded_by=grader,
        )

        client = Client()
        client.force_login(user)
        response = client.get(reverse("scoring:inject_grades_review"))

        assert response.status_code == 200
        page_obj = response.context["page_obj"]

        # Should have 3 grades total
        assert len(list(page_obj)) == 3

    def test_only_unapproved_grades_shown(self, create_user_with_groups: Callable[..., User]) -> None:
        """Only unapproved grades should be shown in review view (pending filter)."""
        user = create_user_with_groups("gold_user", ["WCComps_GoldTeam"])
        grader = User.objects.create_user(username="grader", password="test123")
        approver = User.objects.create_user(username="approver", password="test123")

        team1 = Team.objects.create(team_number=1, team_name="Team 1")
        team2 = Team.objects.create(team_number=2, team_name="Team 2")

        # Create approved grade
        InjectScore.objects.create(
            team=team1,
            inject_id="INJ-001",
            inject_name="Security Policy",
            max_points=Decimal("100.00"),
            points_awarded=Decimal("85.00"),
            graded_by=grader,
            is_approved=True,
            approved_at=timezone.now(),
            approved_by=approver,
        )

        # Create unapproved grade
        InjectScore.objects.create(
            team=team2,
            inject_id="INJ-001",
            inject_name="Security Policy",
            max_points=Decimal("100.00"),
            points_awarded=Decimal("90.00"),
            graded_by=grader,
        )

        client = Client()
        client.force_login(user)
        # Default status filter is "pending"
        response = client.get(reverse("scoring:inject_grades_review"))

        assert response.status_code == 200
        page_obj = response.context["page_obj"]
        grades = list(page_obj)

        # Should have only the unapproved grade
        assert len(grades) == 1
        assert grades[0].team == team2

    def test_empty_state_when_all_grades_approved(self, create_user_with_groups: Callable[..., User]) -> None:
        """View should handle case when all grades are already approved."""
        user = create_user_with_groups("gold_user", ["WCComps_GoldTeam"])
        grader = User.objects.create_user(username="grader", password="test123")
        approver = User.objects.create_user(username="approver", password="test123")

        team1 = Team.objects.create(team_number=1, team_name="Team 1")

        # Create only approved grades
        InjectScore.objects.create(
            team=team1,
            inject_id="INJ-001",
            inject_name="Security Policy",
            max_points=Decimal("100.00"),
            points_awarded=Decimal("85.00"),
            graded_by=grader,
            is_approved=True,
            approved_at=timezone.now(),
            approved_by=approver,
        )

        client = Client()
        client.force_login(user)
        # Default filter is "pending" which should show no grades
        response = client.get(reverse("scoring:inject_grades_review"))

        assert response.status_code == 200
        page_obj = response.context["page_obj"]
        assert len(list(page_obj)) == 0


@pytest.mark.django_db
class TestOutlierDetection:
    """Test outlier detection for inject grades."""

    def test_outliers_detected_above_mean(self, create_user_with_groups: Callable[..., User]) -> None:
        """Grades > 1.5 std dev above mean should be flagged as outliers."""
        user = create_user_with_groups("gold_user", ["WCComps_GoldTeam"])
        grader = User.objects.create_user(username="grader", password="test123")

        # Create teams
        teams = [Team.objects.create(team_number=i, team_name=f"Team {i}") for i in range(1, 6)]

        # Create grades: 50, 55, 60, 65, 100 (100 is outlier - much higher)
        grades_data = [50, 55, 60, 65, 100]
        for team, points in zip(teams, grades_data, strict=True):
            InjectScore.objects.create(
                team=team,
                inject_id="INJ-001",
                inject_name="Security Policy",
                max_points=Decimal("100.00"),
                points_awarded=Decimal(str(points)),
                graded_by=grader,
            )

        client = Client()
        client.force_login(user)
        response = client.get(reverse("scoring:inject_grades_review"))

        assert response.status_code == 200
        page_obj = response.context["page_obj"]
        grades = list(page_obj)

        # Find the grade with 100 points
        grade_100 = next(g for g in grades if g.points_awarded == Decimal("100"))

        # Check outlier info is attached to the grade
        assert hasattr(grade_100, "is_outlier")
        assert grade_100.is_outlier is True
        assert hasattr(grade_100, "std_devs_from_mean")

    def test_outliers_detected_below_mean(self, create_user_with_groups: Callable[..., User]) -> None:
        """Grades > 1.5 std dev below mean should be flagged as outliers."""
        user = create_user_with_groups("gold_user", ["WCComps_GoldTeam"])
        grader = User.objects.create_user(username="grader", password="test123")

        # Create teams
        teams = [Team.objects.create(team_number=i, team_name=f"Team {i}") for i in range(1, 6)]

        # Create grades: 80, 85, 90, 95, 20 (20 is outlier - much lower)
        grades_data = [80, 85, 90, 95, 20]
        for team, points in zip(teams, grades_data, strict=True):
            InjectScore.objects.create(
                team=team,
                inject_id="INJ-002",
                inject_name="Network Diagram",
                max_points=Decimal("100.00"),
                points_awarded=Decimal(str(points)),
                graded_by=grader,
            )

        client = Client()
        client.force_login(user)
        response = client.get(reverse("scoring:inject_grades_review"))

        assert response.status_code == 200
        page_obj = response.context["page_obj"]
        grades = list(page_obj)

        # Find the grade with 20 points
        grade_20 = next(g for g in grades if g.points_awarded == Decimal("20"))

        assert hasattr(grade_20, "is_outlier")
        assert grade_20.is_outlier is True

    def test_no_outliers_with_consistent_grades(self, create_user_with_groups: Callable[..., User]) -> None:
        """Consistent grades should not be flagged as outliers."""
        user = create_user_with_groups("gold_user", ["WCComps_GoldTeam"])
        grader = User.objects.create_user(username="grader", password="test123")

        teams = [Team.objects.create(team_number=i, team_name=f"Team {i}") for i in range(1, 6)]

        # Create consistent grades: 80, 82, 85, 88, 90
        grades_data = [80, 82, 85, 88, 90]
        for team, points in zip(teams, grades_data, strict=True):
            InjectScore.objects.create(
                team=team,
                inject_id="INJ-003",
                inject_name="Incident Response",
                max_points=Decimal("100.00"),
                points_awarded=Decimal(str(points)),
                graded_by=grader,
            )

        client = Client()
        client.force_login(user)
        response = client.get(reverse("scoring:inject_grades_review"))

        assert response.status_code == 200
        page_obj = response.context["page_obj"]
        grades = list(page_obj)

        # No grades should be flagged as outliers
        for grade in grades:
            assert hasattr(grade, "is_outlier")
            assert grade.is_outlier is False

    def test_outlier_detection_with_insufficient_data(self, create_user_with_groups: Callable[..., User]) -> None:
        """Outlier detection should handle case with < 3 grades gracefully."""
        user = create_user_with_groups("gold_user", ["WCComps_GoldTeam"])
        grader = User.objects.create_user(username="grader", password="test123")

        team1 = Team.objects.create(team_number=1, team_name="Team 1")
        team2 = Team.objects.create(team_number=2, team_name="Team 2")

        # Create only 2 grades (not enough for meaningful std dev)
        InjectScore.objects.create(
            team=team1,
            inject_id="INJ-004",
            inject_name="Small Inject",
            max_points=Decimal("100.00"),
            points_awarded=Decimal("50.00"),
            graded_by=grader,
        )
        InjectScore.objects.create(
            team=team2,
            inject_id="INJ-004",
            inject_name="Small Inject",
            max_points=Decimal("100.00"),
            points_awarded=Decimal("100.00"),
            graded_by=grader,
        )

        client = Client()
        client.force_login(user)
        response = client.get(reverse("scoring:inject_grades_review"))

        assert response.status_code == 200
        page_obj = response.context["page_obj"]
        grades = list(page_obj)

        # Should not flag as outliers with insufficient data
        for grade in grades:
            assert hasattr(grade, "is_outlier")
            assert grade.is_outlier is False


@pytest.mark.django_db
class TestBulkApproval:
    """Test bulk approval functionality for inject grades."""

    def test_bulk_approve_multiple_grades(self, create_user_with_groups: Callable[..., User]) -> None:
        """Should be able to bulk approve multiple grades at once."""
        user = create_user_with_groups("gold_user", ["WCComps_GoldTeam"])
        grader = User.objects.create_user(username="grader", password="test123")

        team1 = Team.objects.create(team_number=1, team_name="Team 1")
        team2 = Team.objects.create(team_number=2, team_name="Team 2")
        team3 = Team.objects.create(team_number=3, team_name="Team 3")

        grade1 = InjectScore.objects.create(
            team=team1,
            inject_id="INJ-001",
            inject_name="Security Policy",
            max_points=Decimal("100.00"),
            points_awarded=Decimal("85.00"),
            graded_by=grader,
        )
        grade2 = InjectScore.objects.create(
            team=team2,
            inject_id="INJ-001",
            inject_name="Security Policy",
            max_points=Decimal("100.00"),
            points_awarded=Decimal("90.00"),
            graded_by=grader,
        )
        grade3 = InjectScore.objects.create(
            team=team3,
            inject_id="INJ-001",
            inject_name="Security Policy",
            max_points=Decimal("100.00"),
            points_awarded=Decimal("78.00"),
            graded_by=grader,
        )

        client = Client()
        client.force_login(user)

        # Submit bulk approval
        response = client.post(
            reverse("scoring:inject_grades_bulk_approve"),
            {
                "grade_ids": [grade1.id, grade2.id, grade3.id],
            },
        )

        assert response.status_code == 302  # Redirect after success

        # Check all grades are approved
        grade1.refresh_from_db()
        grade2.refresh_from_db()
        grade3.refresh_from_db()

        assert grade1.is_approved is True
        assert grade1.approved_by == user
        assert grade1.approved_at is not None

        assert grade2.is_approved is True
        assert grade2.approved_by == user
        assert grade2.approved_at is not None

        assert grade3.is_approved is True
        assert grade3.approved_by == user
        assert grade3.approved_at is not None

    def test_bulk_approve_single_grade(self, create_user_with_groups: Callable[..., User]) -> None:
        """Should be able to approve a single grade via bulk approval."""
        user = create_user_with_groups("gold_user", ["WCComps_GoldTeam"])
        grader = User.objects.create_user(username="grader", password="test123")

        team1 = Team.objects.create(team_number=1, team_name="Team 1")

        grade = InjectScore.objects.create(
            team=team1,
            inject_id="INJ-002",
            inject_name="Network Diagram",
            max_points=Decimal("50.00"),
            points_awarded=Decimal("45.00"),
            graded_by=grader,
        )

        client = Client()
        client.force_login(user)

        response = client.post(
            reverse("scoring:inject_grades_bulk_approve"),
            {
                "grade_ids": [grade.id],
            },
        )

        assert response.status_code == 302

        grade.refresh_from_db()
        assert grade.is_approved is True
        assert grade.approved_by == user

    def test_bulk_approve_empty_selection(self, create_user_with_groups: Callable[..., User]) -> None:
        """Should handle empty selection gracefully."""
        user = create_user_with_groups("gold_user", ["WCComps_GoldTeam"])

        client = Client()
        client.force_login(user)

        response = client.post(
            reverse("scoring:inject_grades_bulk_approve"),
            {
                "grade_ids": [],
            },
        )

        # Should redirect with info message
        assert response.status_code == 302

    def test_bulk_approve_only_affects_selected_grades(self, create_user_with_groups: Callable[..., User]) -> None:
        """Bulk approval should only affect selected grades, not all grades."""
        user = create_user_with_groups("gold_user", ["WCComps_GoldTeam"])
        grader = User.objects.create_user(username="grader", password="test123")

        team1 = Team.objects.create(team_number=1, team_name="Team 1")
        team2 = Team.objects.create(team_number=2, team_name="Team 2")

        grade1 = InjectScore.objects.create(
            team=team1,
            inject_id="INJ-003",
            inject_name="Incident Response",
            max_points=Decimal("100.00"),
            points_awarded=Decimal("95.00"),
            graded_by=grader,
        )
        grade2 = InjectScore.objects.create(
            team=team2,
            inject_id="INJ-003",
            inject_name="Incident Response",
            max_points=Decimal("100.00"),
            points_awarded=Decimal("88.00"),
            graded_by=grader,
        )

        client = Client()
        client.force_login(user)

        # Only approve grade1
        response = client.post(
            reverse("scoring:inject_grades_bulk_approve"),
            {
                "grade_ids": [grade1.id],
            },
        )

        assert response.status_code == 302

        grade1.refresh_from_db()
        grade2.refresh_from_db()

        # Only grade1 should be approved
        assert grade1.is_approved is True
        assert grade2.is_approved is False

    def test_white_team_can_bulk_approve(self, create_user_with_groups: Callable[..., User]) -> None:
        """White Team can bulk approve inject grades."""
        white_user = create_user_with_groups("white_user", ["WCComps_WhiteTeam"])
        grader = User.objects.create_user(username="grader", password="test123")

        team1 = Team.objects.create(team_number=1, team_name="Team 1")

        grade = InjectScore.objects.create(
            team=team1,
            inject_id="INJ-004",
            inject_name="Test Inject",
            max_points=Decimal("100.00"),
            points_awarded=Decimal("80.00"),
            graded_by=grader,
        )

        client = Client()
        client.force_login(white_user)

        response = client.post(
            reverse("scoring:inject_grades_bulk_approve"),
            {
                "grade_ids": [grade.id],
            },
        )

        assert response.status_code == 302  # Redirect after approval

        grade.refresh_from_db()
        assert grade.is_approved is True  # White team can now approve

    def test_bulk_approve_invalid_grade_ids(self, create_user_with_groups: Callable[..., User]) -> None:
        """Should handle invalid grade IDs gracefully."""
        user = create_user_with_groups("gold_user", ["WCComps_GoldTeam"])

        client = Client()
        client.force_login(user)

        # Try to approve non-existent grades
        response = client.post(
            reverse("scoring:inject_grades_bulk_approve"),
            {
                "grade_ids": [99999, 88888],
            },
        )

        # Should handle gracefully (redirect with warning or error)
        assert response.status_code == 302

    def test_bulk_approve_already_approved_grades(self, create_user_with_groups: Callable[..., User]) -> None:
        """Should handle case where some grades are already approved."""
        user = create_user_with_groups("gold_user", ["WCComps_GoldTeam"])
        grader = User.objects.create_user(username="grader", password="test123")
        previous_approver = User.objects.create_user(username="previous_approver", password="test123")

        team1 = Team.objects.create(team_number=1, team_name="Team 1")
        team2 = Team.objects.create(team_number=2, team_name="Team 2")

        # Grade already approved
        grade1 = InjectScore.objects.create(
            team=team1,
            inject_id="INJ-005",
            inject_name="Test",
            max_points=Decimal("100.00"),
            points_awarded=Decimal("85.00"),
            graded_by=grader,
            is_approved=True,
            approved_at=timezone.now(),
            approved_by=previous_approver,
        )

        # Grade not yet approved
        grade2 = InjectScore.objects.create(
            team=team2,
            inject_id="INJ-005",
            inject_name="Test",
            max_points=Decimal("100.00"),
            points_awarded=Decimal("90.00"),
            graded_by=grader,
        )

        client = Client()
        client.force_login(user)

        response = client.post(
            reverse("scoring:inject_grades_bulk_approve"),
            {
                "grade_ids": [grade1.id, grade2.id],
            },
        )

        assert response.status_code == 302

        grade1.refresh_from_db()
        grade2.refresh_from_db()

        # grade1 should remain approved by previous_approver
        assert grade1.is_approved is True
        assert grade1.approved_by == previous_approver

        # grade2 should be newly approved
        assert grade2.is_approved is True
        assert grade2.approved_by == user


@pytest.mark.django_db
class TestInjectScoresReviewStats:
    """Test statistics displayed in inject grades review view."""

    def test_stats_show_unapproved_count(self, create_user_with_groups: Callable[..., User]) -> None:
        """View should show count of unapproved grades."""
        user = create_user_with_groups("gold_user", ["WCComps_GoldTeam"])
        grader = User.objects.create_user(username="grader", password="test123")
        approver = User.objects.create_user(username="approver", password="test123")

        team1 = Team.objects.create(team_number=1, team_name="Team 1")
        team2 = Team.objects.create(team_number=2, team_name="Team 2")
        team3 = Team.objects.create(team_number=3, team_name="Team 3")

        # 2 unapproved grades
        InjectScore.objects.create(
            team=team1,
            inject_id="INJ-001",
            inject_name="Test",
            max_points=Decimal("100.00"),
            points_awarded=Decimal("85.00"),
            graded_by=grader,
        )
        InjectScore.objects.create(
            team=team2,
            inject_id="INJ-001",
            inject_name="Test",
            max_points=Decimal("100.00"),
            points_awarded=Decimal("90.00"),
            graded_by=grader,
        )

        # 1 approved grade
        InjectScore.objects.create(
            team=team3,
            inject_id="INJ-001",
            inject_name="Test",
            max_points=Decimal("100.00"),
            points_awarded=Decimal("95.00"),
            graded_by=grader,
            is_approved=True,
            approved_at=timezone.now(),
            approved_by=approver,
        )

        client = Client()
        client.force_login(user)
        response = client.get(reverse("scoring:inject_grades_review"))

        assert response.status_code == 200
        assert response.context["unapproved_count"] == 2
        assert response.context["total_grades"] == 3
        assert response.context["approved_count"] == 1
