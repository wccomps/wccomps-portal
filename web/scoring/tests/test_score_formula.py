"""calculator.py: the score formula's parts each reach a team's total and standing, and an incident's suggested red
findings."""

from collections.abc import Callable
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


def _team(number: int, service: str | None = None) -> Team:
    team = Team.objects.create(team_number=number, team_name=f"Team {number}", is_active=True)
    if service is not None:
        ServiceScore.objects.create(team=team, service_points=Decimal(service))
    return team


def _inject(team: Team, inject_id: str, points: str) -> InjectScore:
    return InjectScore.objects.create(
        team=team, inject_id=inject_id, inject_name=inject_id, points_awarded=Decimal(points), is_approved=True
    )


def _template(service: str, inject: str, orange: str, service_max: str, inject_max: str) -> ScoringTemplate:
    return ScoringTemplate.objects.create(
        service_weight=Decimal(service),
        inject_weight=Decimal(inject),
        orange_weight=Decimal(orange),
        service_max=Decimal(service_max),
        inject_max=Decimal(inject_max),
    )


def _standing(team: Team) -> Standing:
    return get_standing(team.team_number, compute_standings())


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


@pytest.mark.usefixtures("unit_modifiers")
def test_recovery_and_ticket_charges_reach_the_total() -> None:
    team = _team(1, "100")
    _incident(team, points_returned=Decimal("30"), is_approved=True)
    Ticket.objects.create(
        ticket_number="T-0001", team=team, title="t", status="resolved", points_charged=20, is_approved=True
    )

    standing = _standing(team)

    assert standing.incident_recovery_points == Decimal("30")
    assert standing.point_adjustments == Decimal("-20")
    assert standing.total_score == Decimal("110.00")


@pytest.mark.usefixtures("unit_modifiers")
@pytest.mark.parametrize(
    ("points", "expected"), [(("10.25", "0.25"), Decimal("11")), (("10.24", "0.25"), Decimal("10"))]
)
def test_inject_points_total_rounds_half_up_to_whole_points(points: tuple[str, str], expected: Decimal) -> None:
    team = _team(1)
    for number, value in enumerate(points):
        _inject(team, f"inj-{number}", value)

    standing = _standing(team)

    assert standing.scaling["inject_raw"] == expected
    assert standing.total_score == expected


@pytest.mark.parametrize(
    ("weights_and_maxes", "service", "inject", "expected"),
    [
        # Pool = max(1000 / 0.5, 100 / 0.5) = 2000: service scales by 1, inject by 10.
        (("50", "50", "0", "1000", "100"), "500", "50", ("500.00", "500.00", "1000.00")),
        # Inject weighted 0 scales by 0.
        (("100", "0", "0", "100", "100"), "40", "30", ("40.00", "0.00", "40.00")),
        # Inject's 10 / 0.01 = 1000 beats service's 99 / 0.99 = 100: service scales by 0.99 * 1000 / 99 = 10.
        (("99", "1", "0", "99", "10"), "5", "3", ("50.00", "3.00", "53.00")),
        (("50", "50", "0", "1", "1"), "1", "1", ("1.00", "1.00", "2.00")),
    ],
    ids=[
        "the-largest-max-per-weight-sets-the-scale",
        "a-zero-weight-adds-nothing",
        "a-one-percent-weight-still-scales-and-can-set-the-pool",
        "a-raw-max-of-one-point-still-scales",
    ],
)
def test_each_category_scales_to_its_share_of_the_pool(
    weights_and_maxes: tuple[str, str, str, str, str], service: str, inject: str, expected: tuple[str, str, str]
) -> None:
    _template(*weights_and_maxes)
    team = _team(1, service)
    _inject(team, "inj", inject)

    standing = _standing(team)

    assert (standing.service_points, standing.inject_points, standing.total_score) == tuple(map(Decimal, expected))


def _orange(team: Team, points: str) -> OrangeTeamScore:
    return OrangeTeamScore.objects.create(team=team, description="x", points_awarded=Decimal(points), is_approved=True)


@pytest.mark.parametrize(
    ("weights_and_maxes", "score", "category", "expected"),
    [
        # Inject sets the pool (150 / 0.5 = 300): service scales by 0.5 * 300 / 100 = 1.5. 100.03 * 1.5 = 150.045
        (
            ("50", "50", "0", "100", "150"),
            lambda team: ServiceScore.objects.create(team=team, service_points=Decimal("100.03")),
            "service",
            "150.05",
        ),
        # Service sets the pool (1125 / 0.5 = 2250): inject scales by 0.5 * 2250 / 1000 = 1.125. 1 * 1.125 = 1.125
        (("50", "50", "0", "1125", "1000"), lambda team: _inject(team, "inj", "1"), "inject", "1.13"),
        # Service sets the pool (150 / 0.5 = 300): orange scales by 0.5 * 300 / 100 = 1.5. 0.03 * 1.5 = 0.045
        (("50", "0", "50", "150", "100"), lambda team: _orange(team, "0.03"), "orange", "0.05"),
    ],
    ids=["service", "inject", "orange"],
)
def test_scaled_points_round_half_up_to_the_cent(
    weights_and_maxes: tuple[str, str, str, str, str],
    score: Callable[[Team], object],
    category: str,
    expected: str,
) -> None:
    OrangeCheck.objects.create(title="Orange", max_points=100)
    _template(*weights_and_maxes)
    team = _team(1)
    score(team)

    standing = _standing(team)

    assert getattr(standing, f"{category}_points") == Decimal(expected)
    assert standing.total_score == Decimal(expected)


@pytest.mark.usefixtures("unit_modifiers")
def test_get_standing_finds_the_team_or_404() -> None:
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
