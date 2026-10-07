"""Tests for scorecard functionality."""

from decimal import Decimal

import pytest
from django.test import Client
from django.urls import reverse

from scoring.calculator import Standing
from scoring.models import InjectScore, ServiceDetail
from team.models import Team

pytestmark = pytest.mark.django_db


@pytest.fixture
def teams():
    """Create test teams."""
    return [
        Team.objects.create(team_number=1, team_name="Team A", is_active=True),
        Team.objects.create(team_number=2, team_name="Team B", is_active=True),
        Team.objects.create(team_number=3, team_name="Team C", is_active=True),
    ]


def _standing(team, rank, total, service, inject, orange, red, sla, is_excluded=False):
    return Standing(
        team=team,
        service_points=Decimal(service),
        inject_points=Decimal(inject),
        orange_points=Decimal(orange),
        red_deductions=Decimal(red),
        sla_penalties=Decimal(sla),
        point_adjustments=Decimal("0"),
        incident_recovery_points=Decimal("0"),
        total_score=Decimal(total),
        is_excluded=is_excluded,
        rank=rank,
    )


@pytest.fixture
def scores(teams):
    """Standings for all teams."""
    return [
        _standing(teams[0], 2, "15300", "8000", "5000", "3000", "-500", "-200"),
        _standing(teams[1], 1, "19800", "10000", "6000", "4000", "-100", "-100"),
        _standing(teams[2], 3, "9700", "6000", "3000", "2000", "-800", "-500"),
    ]


def _exclude_third(scores):
    third = scores[2]
    third.is_excluded = True
    third.rank = None


class TestComputeScorecardStats:
    """Tests for _compute_scorecard_stats function."""

    def test_compute_category_ranks(self, teams, scores):
        from scoring.views import _compute_scorecard_stats

        stats = _compute_scorecard_stats(scores[0], scores)

        assert stats["team_count"] == 3
        assert stats["category_ranks"]["services"]["rank"] == 2
        assert stats["category_ranks"]["services"]["avg"] == Decimal("8000")
        assert stats["category_ranks"]["injects"]["rank"] == 2
        assert stats["category_ranks"]["orange"]["rank"] == 2

        # Red values stored as absolute (positive), max = most deductions
        red = stats["category_ranks"]["red"]
        assert red["value"] == Decimal("500")  # abs(-500)
        assert red["max"] == Decimal("800")  # abs(min(-800)) = most deductions
        assert red["min"] == Decimal("100")  # abs(max(-100)) = least deductions

    def test_compute_inject_stats(self, teams, scores):
        from scoring.views import _compute_scorecard_stats

        # Create per-inject grades for all teams
        for t, pts in [(teams[0], 80), (teams[1], 100), (teams[2], 60)]:
            InjectScore.objects.create(
                team=t,
                inject_id="inj-1",
                inject_name="Inject 1",
                points_awarded=Decimal(str(pts)),
                is_approved=True,
            )
        for t, pts in [(teams[0], 90), (teams[1], 70), (teams[2], 50)]:
            InjectScore.objects.create(
                team=t,
                inject_id="inj-2",
                inject_name="Inject 2",
                points_awarded=Decimal(str(pts)),
                is_approved=True,
            )
        # qualifier-total should be excluded from inject_stats
        InjectScore.objects.create(
            team=teams[0],
            inject_id="qualifier-total",
            inject_name="Qualifier Total",
            points_awarded=Decimal("170"),
            is_approved=True,
        )

        stats = _compute_scorecard_stats(scores[0], scores)

        assert len(stats["inject_stats"]) == 2
        inj1 = next(s for s in stats["inject_stats"] if s["name"] == "Inject 1")
        assert inj1["points"] == Decimal("80")
        assert inj1["rank"] == 2
        assert inj1["avg"] == Decimal("80")
        inj2 = next(s for s in stats["inject_stats"] if s["name"] == "Inject 2")
        assert inj2["points"] == Decimal("90")
        assert inj2["rank"] == 1

    def test_inject_stats_excludes_unranked_teams(self, teams, scores):
        """Unranked teams (rank=None) should not affect inject detail stats."""
        from scoring.views import _compute_scorecard_stats

        unranked_team = Team.objects.create(team_number=50, team_name="Unranked", is_active=True)
        scores.append(_standing(unranked_team, None, "0", "0", "0", "0", "0", "0"))

        # Ranked teams: inj-1 = 80, 100, 60 → avg 80
        for t, pts in [(teams[0], 80), (teams[1], 100), (teams[2], 60)]:
            InjectScore.objects.create(
                team=t,
                inject_id="inj-1",
                inject_name="Inject 1",
                points_awarded=Decimal(str(pts)),
                is_approved=True,
            )
        # Unranked team drags down average if included
        InjectScore.objects.create(
            team=unranked_team,
            inject_id="inj-1",
            inject_name="Inject 1",
            points_awarded=Decimal("0"),
            is_approved=True,
        )

        stats = _compute_scorecard_stats(scores[0], scores)

        inj1 = stats["inject_stats"][0]
        # avg should be (80+100+60)/3 = 80, not (80+100+60+0)/4 = 60
        assert inj1["avg"] == Decimal("80")
        assert inj1["rank"] == 2

    def test_inject_stats_excludes_excluded_teams(self, teams, scores):
        from scoring.views import _compute_scorecard_stats

        for t, pts in [(teams[0], 80), (teams[1], 100), (teams[2], 60)]:
            InjectScore.objects.create(
                team=t,
                inject_id="inj-1",
                inject_name="Inject 1",
                points_awarded=Decimal(str(pts)),
                is_approved=True,
            )
        _exclude_third(scores)

        stats = _compute_scorecard_stats(scores[0], scores)

        inj1 = stats["inject_stats"][0]
        # avg should be (80+100)/2 = 90, not (80+100+60)/3
        assert inj1["avg"] == Decimal("90")
        assert inj1["rank"] == 2

    def test_compute_neighbors(self, teams, scores):
        from scoring.views import _compute_scorecard_stats

        # teams[0] is rank 2 (middle), should have neighbors above and below
        stats = _compute_scorecard_stats(scores[0], scores)

        assert len(stats["neighbors"]) == 2
        above = next(n for n in stats["neighbors"] if n["rank"] == 1)
        below = next(n for n in stats["neighbors"] if n["rank"] == 3)
        assert above["total_score"] == Decimal("19800")
        assert above["gap"] == Decimal("4500")
        assert below["total_score"] == Decimal("9700")
        assert below["gap"] == Decimal("-5600")

    def test_neighbors_at_top(self, teams, scores):
        from scoring.views import _compute_scorecard_stats

        # teams[1] is rank 1, should only have one neighbor below
        stats = _compute_scorecard_stats(scores[1], scores)

        assert len(stats["neighbors"]) == 1
        assert stats["neighbors"][0]["rank"] == 2

    def test_compute_insights(self, teams, scores):
        from scoring.views import _compute_scorecard_stats

        stats = _compute_scorecard_stats(scores[0], scores)

        assert len(stats["insights"]) >= 1
        assert all(isinstance(i, str) for i in stats["insights"])

    def test_compute_service_stats(self, teams, scores):
        from scoring.views import _compute_scorecard_stats

        for t, pts in [(teams[0], 400), (teams[1], 450), (teams[2], 300)]:
            ServiceDetail.objects.create(team=t, service_name="tahoe-dns", points=Decimal(str(pts)))
        for t, pts in [(teams[0], 200), (teams[1], 350), (teams[2], 250)]:
            ServiceDetail.objects.create(team=t, service_name="berryessa-ssh", points=Decimal(str(pts)))

        stats = _compute_scorecard_stats(scores[0], scores)

        assert len(stats["service_stats"]) == 2
        tahoe = next(s for s in stats["service_stats"] if s["name"] == "tahoe-dns")
        assert tahoe["points"] == Decimal("400")
        assert tahoe["rank"] == 2
        berry = next(s for s in stats["service_stats"] if s["name"] == "berryessa-ssh")
        assert berry["points"] == Decimal("200")
        assert berry["rank"] == 3
        assert berry["below_avg"] is True

    def test_service_stats_excludes_unranked_teams(self, teams, scores):
        from scoring.views import _compute_scorecard_stats

        unranked_team = Team.objects.create(team_number=50, team_name="Unranked", is_active=True)
        scores.append(_standing(unranked_team, None, "0", "0", "0", "0", "0", "0"))

        for t, pts in [(teams[0], 400), (teams[1], 450), (teams[2], 300)]:
            ServiceDetail.objects.create(team=t, service_name="tahoe-dns", points=Decimal(str(pts)))
        ServiceDetail.objects.create(team=unranked_team, service_name="tahoe-dns", points=Decimal("0"))

        stats = _compute_scorecard_stats(scores[0], scores)

        tahoe = stats["service_stats"][0]
        # avg should be (400+450+300)/3, not (400+450+300+0)/4
        assert round(tahoe["avg"], 2) == round(Decimal("1150") / 3, 2)
        assert tahoe["rank"] == 2

    def test_excluded_team_not_in_stats(self, teams, scores):
        from scoring.views import _compute_scorecard_stats

        _exclude_third(scores)

        stats = _compute_scorecard_stats(scores[0], scores)

        assert stats["team_count"] == 2
        assert stats["category_ranks"]["services"]["rank"] == 2
        assert stats["category_ranks"]["services"]["avg"] == Decimal("9000")


class TestScorecardView:
    """Tests for scorecard view."""

    def test_requires_authentication(self, client, teams, scores):
        url = reverse("leaderboard_scorecard", args=[1])
        response = client.get(url)
        assert response.status_code == 302
        assert "login" in response.url

    def test_gold_team_can_access(self, gold_team_user, teams, scores):
        client = Client()
        client.force_login(gold_team_user)
        url = reverse("leaderboard_scorecard", args=[1])
        response = client.get(url)
        assert response.status_code == 200

    def test_orange_team_can_access(self, orange_team_user, teams, scores):
        client = Client()
        client.force_login(orange_team_user)
        url = reverse("leaderboard_scorecard", args=[1])
        response = client.get(url)
        assert response.status_code == 200

    def test_ticketing_support_can_access(self, ticketing_support_user, teams, scores):
        client = Client()
        client.force_login(ticketing_support_user)
        url = reverse("leaderboard_scorecard", args=[1])
        response = client.get(url)
        assert response.status_code == 200

    def test_returns_404_for_missing_team(self, gold_team_user, teams, scores):
        client = Client()
        client.force_login(gold_team_user)
        url = reverse("leaderboard_scorecard", args=[99])
        response = client.get(url)
        assert response.status_code == 404


class TestScorecardRedTeamDetail:
    """Tests for detailed red team findings on scorecard."""

    def test_scorecard_shows_attack_type(self, gold_team_user, teams, scores):
        from scoring.models import AttackType, RedTeamScore

        attack_type, _ = AttackType.objects.get_or_create(name="Default Credentials")
        finding = RedTeamScore.objects.create(
            attack_type=attack_type,
            attack_vector=".240 Default Creds",
            points_per_team=Decimal("100"),
            is_approved=True,
        )
        finding.affected_teams.add(teams[0])

        client = Client()
        client.force_login(gold_team_user)
        response = client.get(reverse("leaderboard_scorecard", args=[1]))

        assert response.status_code == 200
        content = response.content.decode()
        assert "Default Credentials" in content  # attack_type.name shown
        assert ".240 Default Creds" not in content  # raw attack_vector NOT shown

    def test_scorecard_shows_affected_boxes(self, gold_team_user, teams, scores):
        from scoring.models import AttackType, RedTeamScore

        attack_type = AttackType.objects.create(name="RCE")
        finding = RedTeamScore.objects.create(
            attack_type=attack_type,
            affected_boxes=["web-01", "db-02"],
            affected_service="ssh",
            points_per_team=Decimal("100"),
            is_approved=True,
        )
        finding.affected_teams.add(teams[0])

        client = Client()
        client.force_login(gold_team_user)
        response = client.get(reverse("leaderboard_scorecard", args=[1]))

        content = response.content.decode()
        assert "web-01, db-02" in content
        assert "ssh" in content

    def test_scorecard_shows_outcome_flags(self, gold_team_user, teams, scores):
        from scoring.models import AttackType, RedTeamScore

        attack_type, _ = AttackType.objects.get_or_create(name="Privilege Escalation")
        finding = RedTeamScore.objects.create(
            attack_type=attack_type,
            root_access=True,
            credentials_recovered=True,
            points_per_team=Decimal("150"),
            is_approved=True,
        )
        finding.affected_teams.add(teams[0])

        client = Client()
        client.force_login(gold_team_user)
        response = client.get(reverse("leaderboard_scorecard", args=[1]))

        content = response.content.decode()
        assert "Root Access (-100)" in content
        assert "Credentials (-50)" in content


class TestScorecardScalingContext:
    """Tests for scaling context footnote on scorecard."""

    def test_scorecard_shows_scaling_weights(self, gold_team_user, teams, scores):
        from scoring.models import ScoringTemplate

        ScoringTemplate.objects.create(
            service_weight=Decimal("40"),
            inject_weight=Decimal("40"),
            orange_weight=Decimal("20"),
            service_max=Decimal("11454"),
            inject_max=Decimal("3060"),
            orange_max=Decimal("160"),
        )

        client = Client()
        client.force_login(gold_team_user)
        response = client.get(reverse("leaderboard_scorecard", args=[1]))

        content = response.content.decode()
        assert "Service 40%" in content
        assert "Inject 40%" in content
        assert "Orange Checks 20%" in content


class TestScorecardPdf:
    """Tests for PDF scorecard export."""

    def test_pdf_returns_pdf_content_type(self, gold_team_user, teams, scores):
        client = Client()
        client.force_login(gold_team_user)
        url = reverse("leaderboard_scorecard_pdf", args=[1])
        response = client.get(url)

        assert response.status_code == 200
        assert response["Content-Type"] == "application/pdf"
        assert 'filename="team-01-scorecard.pdf"' in response["Content-Disposition"]

    def test_orange_team_can_access(self, orange_team_user, teams, scores):
        client = Client()
        client.force_login(orange_team_user)
        url = reverse("leaderboard_scorecard_pdf", args=[1])
        response = client.get(url)

        assert response.status_code == 200
        assert response["Content-Type"] == "application/pdf"
        assert 'filename="team-01-scorecard.pdf"' in response["Content-Disposition"]

    def test_ticketing_support_can_access(self, ticketing_support_user, teams, scores):
        client = Client()
        client.force_login(ticketing_support_user)
        url = reverse("leaderboard_scorecard_pdf", args=[1])
        response = client.get(url)

        assert response.status_code == 200
        assert response["Content-Type"] == "application/pdf"
        assert 'filename="team-01-scorecard.pdf"' in response["Content-Disposition"]

    def test_pdf_requires_authentication(self, client, teams, scores):
        url = reverse("leaderboard_scorecard_pdf", args=[1])
        response = client.get(url)
        assert response.status_code == 302

    def test_pdf_returns_404_for_missing_team(self, gold_team_user, teams, scores):
        client = Client()
        client.force_login(gold_team_user)
        url = reverse("leaderboard_scorecard_pdf", args=[99])
        response = client.get(url)
        assert response.status_code == 404
