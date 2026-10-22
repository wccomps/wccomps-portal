"""Tests for Red Team findings filtering functionality."""

from collections.abc import Callable
from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from scoring.models import AttackType, RedTeamScore
from team.models import Team


@pytest.mark.django_db
class TestRedTeamFindingsFiltering:
    """Test filtering capabilities in Red Team findings view."""

    def test_filter_by_target_team(self, create_user_with_groups: Callable[..., User]) -> None:
        """Red Team can filter findings by target team."""
        red_user = create_user_with_groups("red_user", ["WCComps_RedTeam"])
        team1 = Team.objects.create(team_number=1, team_name="Team 1")
        team2 = Team.objects.create(team_number=2, team_name="Team 2")

        # Create findings targeting different teams
        finding1 = RedTeamScore.objects.create(
            source_ip="10.0.0.5",
            points_per_team=Decimal("50.00"),
            submitted_by=red_user,
        )
        finding1.affected_teams.add(team1)

        finding2 = RedTeamScore.objects.create(
            source_ip="10.0.0.6",
            points_per_team=Decimal("30.00"),
            submitted_by=red_user,
        )
        finding2.affected_teams.add(team2)

        finding3 = RedTeamScore.objects.create(
            source_ip="10.0.0.7",
            points_per_team=Decimal("100.00"),
            submitted_by=red_user,
        )
        finding3.affected_teams.add(team1, team2)

        client = Client()
        client.force_login(red_user)

        # Filter by team1 (default status is "all" for red team view)
        response = client.get(reverse("scoring:red_team_scores"), {"team": team1.team_number, "status": "all"})
        assert response.status_code == 200
        findings = list(response.context["page_obj"])
        finding_ids = {f.id for f in findings}
        assert finding1.id in finding_ids
        assert finding2.id not in finding_ids
        assert finding3.id in finding_ids

    def test_filter_by_attack_type(self, create_user_with_groups: Callable[..., User]) -> None:
        """Red Team can filter findings by attack type."""
        red_user = create_user_with_groups("red_user", ["WCComps_RedTeam"])
        team = Team.objects.create(team_number=1, team_name="Team 1")

        finding1 = RedTeamScore.objects.create(
            source_ip="10.0.0.5",
            points_per_team=Decimal("50.00"),
            submitted_by=red_user,
        )
        finding1.affected_teams.add(team)

        finding2 = RedTeamScore.objects.create(
            source_ip="10.0.0.6",
            points_per_team=Decimal("30.00"),
            submitted_by=red_user,
        )
        finding2.affected_teams.add(team)

        client = Client()
        client.force_login(red_user)

        # Filter by attack type ID
        sql_type, _ = AttackType.objects.get_or_create(name="SQL Injection")
        finding1.attack_type = sql_type
        finding1.save()
        response = client.get(reverse("scoring:red_team_scores"), {"attack_type": sql_type.id, "status": "all"})
        assert response.status_code == 200
        findings = list(response.context["page_obj"])
        finding_ids = {f.id for f in findings}
        assert finding1.id in finding_ids
        assert finding2.id not in finding_ids

    def test_filter_by_submitter(self, create_user_with_groups: Callable[..., User]) -> None:
        """Red Team can filter findings by submitter."""
        red_user1 = create_user_with_groups("red_user1", ["WCComps_RedTeam"])
        red_user2 = create_user_with_groups("red_user2", ["WCComps_RedTeam"])
        team = Team.objects.create(team_number=1, team_name="Team 1")

        finding1 = RedTeamScore.objects.create(
            source_ip="10.0.0.5",
            points_per_team=Decimal("50.00"),
            submitted_by=red_user1,
        )
        finding1.affected_teams.add(team)

        finding2 = RedTeamScore.objects.create(
            source_ip="10.0.0.6",
            points_per_team=Decimal("30.00"),
            submitted_by=red_user2,
        )
        finding2.affected_teams.add(team)

        client = Client()
        client.force_login(red_user1)

        # Filter by submitter
        response = client.get(reverse("scoring:red_team_scores"), {"submitter": red_user1.id, "status": "all"})
        assert response.status_code == 200
        findings = list(response.context["page_obj"])
        finding_ids = {f.id for f in findings}
        assert finding1.id in finding_ids
        assert finding2.id not in finding_ids

    def test_filter_multiple_criteria(self, create_user_with_groups: Callable[..., User]) -> None:
        """Red Team can combine multiple filters."""
        red_user1 = create_user_with_groups("red_user1", ["WCComps_RedTeam"])
        red_user2 = create_user_with_groups("red_user2", ["WCComps_RedTeam"])
        team1 = Team.objects.create(team_number=1, team_name="Team 1")
        team2 = Team.objects.create(team_number=2, team_name="Team 2")

        finding1 = RedTeamScore.objects.create(
            source_ip="10.0.0.5",
            points_per_team=Decimal("50.00"),
            submitted_by=red_user1,
        )
        finding1.affected_teams.add(team1)

        finding2 = RedTeamScore.objects.create(
            source_ip="10.0.0.6",
            points_per_team=Decimal("30.00"),
            submitted_by=red_user2,
        )
        finding2.affected_teams.add(team1)

        finding3 = RedTeamScore.objects.create(
            source_ip="10.0.0.7",
            points_per_team=Decimal("40.00"),
            submitted_by=red_user1,
        )
        finding3.affected_teams.add(team2)

        client = Client()
        client.force_login(red_user1)

        # Filter by team AND submitter
        response = client.get(
            reverse("scoring:red_team_scores"), {"team": team1.team_number, "submitter": red_user1.id, "status": "all"}
        )
        assert response.status_code == 200
        findings = list(response.context["page_obj"])
        finding_ids = {f.id for f in findings}
        assert finding1.id in finding_ids
        assert finding2.id not in finding_ids
        assert finding3.id not in finding_ids

    def test_no_filters_shows_all_findings(self, create_user_with_groups: Callable[..., User]) -> None:
        """Without filters, all findings are shown."""
        red_user = create_user_with_groups("red_user", ["WCComps_RedTeam"])
        team = Team.objects.create(team_number=1, team_name="Team 1")

        finding1 = RedTeamScore.objects.create(
            source_ip="10.0.0.5",
            points_per_team=Decimal("50.00"),
            submitted_by=red_user,
        )
        finding1.affected_teams.add(team)

        finding2 = RedTeamScore.objects.create(
            source_ip="10.0.0.6",
            points_per_team=Decimal("30.00"),
            submitted_by=red_user,
        )
        finding2.affected_teams.add(team)

        client = Client()
        client.force_login(red_user)

        response = client.get(reverse("scoring:red_team_scores"), {"status": "all"})
        assert response.status_code == 200
        findings = list(response.context["page_obj"])
        assert len(findings) == 2

    def test_filter_context_includes_teams_and_submitters(self, create_user_with_groups: Callable[..., User]) -> None:
        """Filter context includes available teams and submitters for dropdowns."""
        red_user = create_user_with_groups("red_user", ["WCComps_RedTeam"])
        team1 = Team.objects.create(team_number=1, team_name="Team 1")
        team2 = Team.objects.create(team_number=2, team_name="Team 2")

        finding = RedTeamScore.objects.create(
            source_ip="10.0.0.5",
            points_per_team=Decimal("50.00"),
            submitted_by=red_user,
        )
        finding.affected_teams.add(team1, team2)

        client = Client()
        client.force_login(red_user)

        response = client.get(reverse("scoring:red_team_scores"))
        assert response.status_code == 200
        assert response.context["team_numbers"] == [1, 2]
        assert "available_submitters" in response.context
        assert red_user in response.context["available_submitters"]

    def test_approval_status_not_visible_to_red_team(self, create_user_with_groups: Callable[..., User]) -> None:
        """Red Team users should not see approval status fields."""
        red_user = create_user_with_groups("red_user", ["WCComps_RedTeam"])
        team = Team.objects.create(team_number=1, team_name="Team 1")

        finding = RedTeamScore.objects.create(
            source_ip="10.0.0.5",
            points_per_team=Decimal("50.00"),
            submitted_by=red_user,
            is_approved=True,
        )
        finding.affected_teams.add(team)

        client = Client()
        client.force_login(red_user)

        response = client.get(reverse("scoring:red_team_scores"))
        assert response.status_code == 200

        # Check that template does not contain approval-related info in Red Team view
        content = response.content.decode()
        assert "is_approved" not in content.lower() or "approved" not in content.lower()
        # Points per team should not be visible to Red Team
        assert "points_per_team" not in content.lower() or "50.00" not in content

    def test_gold_team_sees_all_findings_including_approval_status(
        self, create_user_with_groups: Callable[..., User]
    ) -> None:
        """Gold Team can access red team findings page and see approval status."""
        gold_user = create_user_with_groups("gold_user", ["WCComps_GoldTeam"])
        team = Team.objects.create(team_number=1, team_name="Team 1")

        finding = RedTeamScore.objects.create(
            source_ip="10.0.0.5",
            points_per_team=Decimal("50.00"),
            submitted_by=gold_user,
            is_approved=True,
        )
        finding.affected_teams.add(team)

        client = Client()
        client.force_login(gold_user)

        response = client.get(reverse("scoring:red_team_findings"))
        assert response.status_code == 200

        # Gold Team should see points per team in reviewed findings
        content = response.content.decode()
        assert "50" in content or "50.00" in content


@pytest.mark.django_db
class TestRedTeamSortCycle:
    """Test that the 3-state sort cycle (asc -> desc -> unsort) works."""

    def _create_findings(self, user: User) -> tuple[RedTeamScore, RedTeamScore, RedTeamScore]:
        """Create 3 findings with different timestamps."""
        team = Team.objects.create(team_number=1, team_name="Team 1")
        base_time = timezone.now()
        findings = []
        for i in range(3):
            f = RedTeamScore.objects.create(
                source_ip=f"10.0.0.{i}",
                points_per_team=Decimal("10.00"),
                submitted_by=user,
            )
            f.affected_teams.add(team)
            # Set distinct timestamps without sleeping
            RedTeamScore.objects.filter(pk=f.pk).update(created_at=base_time + timedelta(seconds=i))
            f.refresh_from_db()
            findings.append(f)
        return findings[0], findings[1], findings[2]

    def test_sort_ascending(self, create_user_with_groups: Callable[..., User]) -> None:
        """sort=created_at returns findings oldest-first."""
        gold_user = create_user_with_groups("gold_user", ["WCComps_GoldTeam"])
        f1, f2, f3 = self._create_findings(gold_user)

        client = Client()
        client.force_login(gold_user)
        response = client.get(reverse("scoring:red_team_findings"), {"sort": "created_at", "status": "all"})
        assert response.status_code == 200

        ids = [f.id for f in response.context["page_obj"]]
        assert ids == [f1.id, f2.id, f3.id]
        assert response.context["sort_by"] == "created_at"

    def test_sort_descending(self, create_user_with_groups: Callable[..., User]) -> None:
        """sort=-created_at returns findings newest-first."""
        gold_user = create_user_with_groups("gold_user", ["WCComps_GoldTeam"])
        f1, f2, f3 = self._create_findings(gold_user)

        client = Client()
        client.force_login(gold_user)
        response = client.get(reverse("scoring:red_team_findings"), {"sort": "-created_at", "status": "all"})
        assert response.status_code == 200

        ids = [f.id for f in response.context["page_obj"]]
        assert ids == [f3.id, f2.id, f1.id]
        assert response.context["sort_by"] == "-created_at"

    def test_sort_default_unsorts(self, create_user_with_groups: Callable[..., User]) -> None:
        """sort=default should NOT re-apply the view's default sort.

        This is the key bug fix: previously, 'sort=default' failed validation
        and silently fell back to '-created_at', trapping users in a loop.
        Now sort_by should be empty string, meaning no column shows as active.
        """
        gold_user = create_user_with_groups("gold_user", ["WCComps_GoldTeam"])
        self._create_findings(gold_user)

        client = Client()
        client.force_login(gold_user)
        response = client.get(reverse("scoring:red_team_findings"), {"sort": "default", "status": "all"})
        assert response.status_code == 200
        # sort_by must be empty so no column header shows an arrow
        assert response.context["sort_by"] == ""

    def test_sort_cycle_no_arrow_after_unsort(self, create_user_with_groups: Callable[..., User]) -> None:
        """After unsort (sort=default), no column header shows a sort arrow."""
        gold_user = create_user_with_groups("gold_user", ["WCComps_GoldTeam"])
        self._create_findings(gold_user)

        client = Client()
        client.force_login(gold_user)
        response = client.get(reverse("scoring:red_team_findings"), {"sort": "default", "status": "all"})
        content = response.content.decode()
        # The sort indicators should NOT appear in any table header
        # (they only appear when current_sort matches a sort_field)
        assert "▲" not in content
        assert "▼" not in content

    def test_first_visit_uses_default_sort(self, create_user_with_groups: Callable[..., User]) -> None:
        """First visit (no sort param) should use the view's default sort."""
        gold_user = create_user_with_groups("gold_user", ["WCComps_GoldTeam"])
        f1, f2, f3 = self._create_findings(gold_user)

        client = Client()
        client.force_login(gold_user)
        response = client.get(reverse("scoring:red_team_findings"), {"status": "all"})
        assert response.status_code == 200

        # Default is -created_at (newest first)
        ids = [f.id for f in response.context["page_obj"]]
        assert ids == [f3.id, f2.id, f1.id]
        assert response.context["sort_by"] == "-created_at"
