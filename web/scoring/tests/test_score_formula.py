"""The score formula's parts each reach a team's total and standing."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.http import Http404
from django.utils import timezone
from orange_team.models import OrangeCheck

from scoring.calculator import Standing, compute_standings, get_standing, suggest_red_score_matches
from scoring.models import (
    IncidentReport,
    InjectScore,
    OrangeTeamScore,
    RedTeamIPPool,
    RedTeamScore,
    ScoringTemplate,
    ServiceScore,
)
from team.models import Team
from ticketing.models import Ticket

pytestmark = pytest.mark.django_db


def _unit_template() -> ScoringTemplate:
    # Service and inject maxes proportional to their weights and no orange checks: both modifiers are exactly 1.
    return ScoringTemplate.objects.create(
        service_weight=Decimal("50"),
        inject_weight=Decimal("50"),
        orange_weight=Decimal("0"),
        service_max=Decimal("100"),
        inject_max=Decimal("100"),
    )


def _team(number: int, service: str | None = None) -> Team:
    team = Team.objects.create(team_number=number, team_name=f"Team {number}", is_active=True)
    if service is not None:
        ServiceScore.objects.create(team=team, service_points=Decimal(service))
    return team


def _inject(team: Team, inject_id: str, points: str) -> InjectScore:
    return InjectScore.objects.create(
        team=team, inject_id=inject_id, inject_name=inject_id, points_awarded=Decimal(points), is_approved=True
    )


def _standing(team: Team) -> Standing:
    return get_standing(team.team_number, compute_standings())


def test_recovery_and_ticket_charges_reach_the_total() -> None:
    _unit_template()
    team = _team(1, "100")
    IncidentReport.objects.create(
        team=team,
        submitted_by=User.objects.create(username="blue1"),
        attack_description="x",
        source_ip="10.0.0.1",
        attack_detected_at=timezone.now(),
        points_returned=Decimal("30"),
        is_approved=True,
    )
    Ticket.objects.create(
        ticket_number="T-0001", team=team, title="t", status="resolved", points_charged=20, is_approved=True
    )

    standing = _standing(team)

    assert standing.incident_recovery_points == Decimal("30")
    assert standing.point_adjustments == Decimal("-20")
    assert standing.total_score == Decimal("110.00")


@pytest.mark.parametrize(
    ("points", "expected"), [(("10.25", "0.25"), Decimal("11")), (("10.24", "0.25"), Decimal("10"))]
)
def test_inject_points_total_rounds_half_up_to_whole_points(points: tuple[str, str], expected: Decimal) -> None:
    _unit_template()
    team = _team(1)
    for number, value in enumerate(points):
        _inject(team, f"inj-{number}", value)

    standing = _standing(team)

    assert standing.scaling["inject_raw"] == expected
    assert standing.total_score == expected


def test_largest_max_per_weight_sets_the_scale() -> None:
    # Pool = max(1000 / 0.5, 100 / 0.5) = 2000, so service scales by 1 and inject by 10.
    ScoringTemplate.objects.create(
        service_weight=Decimal("50"),
        inject_weight=Decimal("50"),
        orange_weight=Decimal("0"),
        service_max=Decimal("1000"),
        inject_max=Decimal("100"),
    )
    team = _team(1, "500")
    _inject(team, "inj", "50")

    standing = _standing(team)

    assert (standing.scaling["service_modifier"], standing.scaling["inject_modifier"]) == (Decimal("1"), Decimal("10"))
    assert standing.service_points == Decimal("500.00")
    assert standing.inject_points == Decimal("500.00")
    assert standing.total_score == Decimal("1000.00")


def test_category_with_zero_weight_adds_nothing() -> None:
    ScoringTemplate.objects.create(
        service_weight=Decimal("100"),
        inject_weight=Decimal("0"),
        orange_weight=Decimal("0"),
        service_max=Decimal("100"),
        inject_max=Decimal("100"),
    )
    team = _team(1, "40")
    _inject(team, "inj", "30")

    standing = _standing(team)

    assert standing.scaling["inject_modifier"] == Decimal("0")
    assert standing.inject_points == Decimal("0.00")
    assert standing.total_score == Decimal("40.00")


def test_a_one_percent_weight_still_scales_and_can_set_the_pool() -> None:
    # Inject's 10 / 0.01 = 1000 beats service's 99 / 0.99 = 100: service scales by 0.99 * 1000 / 99 = 10, inject by 1.
    ScoringTemplate.objects.create(
        service_weight=Decimal("99"),
        inject_weight=Decimal("1"),
        orange_weight=Decimal("0"),
        service_max=Decimal("99"),
        inject_max=Decimal("10"),
    )
    team = _team(1, "5")
    _inject(team, "inj", "3")

    standing = _standing(team)

    assert (standing.service_points, standing.inject_points) == (Decimal("50.00"), Decimal("3.00"))
    assert standing.total_score == Decimal("53.00")


def test_a_raw_max_of_one_point_still_scales() -> None:
    ScoringTemplate.objects.create(
        service_weight=Decimal("50"),
        inject_weight=Decimal("50"),
        orange_weight=Decimal("0"),
        service_max=Decimal("1"),
        inject_max=Decimal("1"),
    )
    team = _team(1, "1")
    _inject(team, "inj", "1")

    standing = _standing(team)

    assert (standing.service_points, standing.inject_points, standing.total_score) == (
        Decimal("1.00"),
        Decimal("1.00"),
        Decimal("2.00"),
    )


def _scale_one_category(category: str) -> Team:
    """A template where `category` scales by a fraction, and a team whose scaled points land on half a cent."""
    weights = {"service_weight": Decimal("50"), "inject_weight": Decimal("0"), "orange_weight": Decimal("0")}
    team = _team(1)
    if category == "service":
        # Inject sets the pool (150 / 0.5 = 300): service scales by 0.5 * 300 / 100 = 1.5. 100.03 * 1.5 = 150.045
        weights["inject_weight"] = Decimal("50")
        ScoringTemplate.objects.create(**weights, service_max=Decimal("100"), inject_max=Decimal("150"))
        ServiceScore.objects.create(team=team, service_points=Decimal("100.03"))
    elif category == "inject":
        # Service sets the pool (1125 / 0.5 = 2250): inject scales by 0.5 * 2250 / 1000 = 1.125. 1 * 1.125 = 1.125
        weights["inject_weight"] = Decimal("50")
        ScoringTemplate.objects.create(**weights, service_max=Decimal("1125"), inject_max=Decimal("1000"))
        _inject(team, "inj", "1")
    else:
        # Service sets the pool (150 / 0.5 = 300): orange scales by 0.5 * 300 / 100 = 1.5. 0.03 * 1.5 = 0.045
        weights["orange_weight"] = Decimal("50")
        OrangeCheck.objects.create(title="Orange", max_points=100)
        ScoringTemplate.objects.create(**weights, service_max=Decimal("150"), inject_max=Decimal("100"))
        OrangeTeamScore.objects.create(team=team, description="x", points_awarded=Decimal("0.03"), is_approved=True)
    return team


@pytest.mark.parametrize(
    ("category", "expected"),
    [("service", Decimal("150.05")), ("inject", Decimal("1.13")), ("orange", Decimal("0.05"))],
)
def test_scaled_points_round_half_up_to_the_cent(category: str, expected: Decimal) -> None:
    standing = _standing(_scale_one_category(category))

    assert getattr(standing, f"{category}_points") == expected
    assert standing.total_score == expected


def test_get_standing_finds_the_team_or_404() -> None:
    _unit_template()
    first, second = _team(1, "10"), _team(2, "20")
    standings = compute_standings()

    assert get_standing(1, standings).team == first
    assert get_standing(2, standings).team == second
    with pytest.raises(Http404):
        get_standing(3, standings)


def _finding(user: User, team: Team, **fields: object) -> RedTeamScore:
    finding = RedTeamScore.objects.create(points_per_team=Decimal("50"), submitted_by=user, **fields)
    finding.affected_teams.add(team)
    return finding


def _incident(team: Team, source_ip: str = "10.9.9.9", **fields: object) -> IncidentReport:
    # An incident always names a source IP; the default one matches no finding.
    blue = User.objects.create(username=f"blue{IncidentReport.objects.count()}")
    return IncidentReport.objects.create(
        team=team,
        submitted_by=blue,
        attack_description="x",
        source_ip=source_ip,
        attack_detected_at=timezone.now(),
        **fields,
    )


def test_suggestions_are_the_incident_teams_findings_sharing_any_detail(gold_team_user: User) -> None:
    team, other = _team(1), _team(2)
    pool = RedTeamIPPool.objects.create(name="p", ip_addresses="10.0.0.8, 10.0.0.9", created_by=gold_team_user)
    RedTeamIPPool.objects.create(name="q", ip_addresses="10.0.0.7", created_by=gold_team_user)
    sharing = {
        _finding(gold_team_user, team, source_ip="10.0.0.9"),
        _finding(gold_team_user, team, source_ip_pool=pool),
        _finding(gold_team_user, team, source_ip="10.0.0.1", affected_boxes=["web"]),
        _finding(gold_team_user, team, source_ip="10.0.0.2", affected_boxes=["db", "mail"]),
        _finding(gold_team_user, team, source_ip="10.0.0.3", affected_service="SSH"),
    }
    _finding(gold_team_user, team, source_ip="10.0.0.4", affected_boxes=["mail"], affected_service="SMTP")
    _finding(gold_team_user, other, source_ip="10.0.0.9", affected_boxes=["web"], affected_service="SSH")

    incident = _incident(team, source_ip="10.0.0.9", affected_boxes=["web", "db"], affected_service="SSH")

    assert set(suggest_red_score_matches(incident)) == sharing


def test_suggestion_by_one_detail_leaves_out_findings_without_it(gold_team_user: User) -> None:
    team = _team(1)
    pool = RedTeamIPPool.objects.create(name="p", ip_addresses="10.0.0.8\n10.0.0.9", created_by=gold_team_user)
    by_ip = _finding(gold_team_user, team, source_ip="10.0.0.5")
    by_pool = _finding(gold_team_user, team, source_ip_pool=pool)
    by_box = _finding(gold_team_user, team, source_ip="10.0.0.1", affected_boxes=["db"])
    by_service = _finding(gold_team_user, team, source_ip="10.0.0.2", affected_service="SSH")

    by_detail = {
        "ip": _incident(team, source_ip="10.0.0.5"),
        "pool": _incident(team, source_ip="10.0.0.9"),
        "box": _incident(team, affected_boxes=["db"]),
        "service": _incident(team, affected_service="SSH"),
    }

    assert {name: set(suggest_red_score_matches(incident)) for name, incident in by_detail.items()} == {
        "ip": {by_ip},
        "pool": {by_pool},
        "box": {by_box},
        "service": {by_service},
    }


def test_suggestions_are_the_ten_newest(gold_team_user: User) -> None:
    team = _team(1)
    findings = [_finding(gold_team_user, team, source_ip="10.0.0.5") for _ in range(11)]
    start = timezone.now()
    for age, finding in enumerate(reversed(findings)):
        RedTeamScore.objects.filter(pk=finding.pk).update(created_at=start - timedelta(minutes=age))

    suggestions = list(suggest_red_score_matches(_incident(team, source_ip="10.0.0.5")))

    assert suggestions == list(reversed(findings))[:10]
