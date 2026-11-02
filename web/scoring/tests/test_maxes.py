from decimal import Decimal

import pytest
from orange_team.models import OrangeCheck, OrangeCheckCriterion
from quotient.client import Inject, inject_points

from scoring.maxes import (
    inject_max_from_titles,
    service_max_from_details,
    set_template_max,
)
from scoring.models import ScoringTemplate, ServiceDetail
from team.models import Team

pytestmark = pytest.mark.django_db


def _injects(*titles: str) -> list[Inject]:
    return [Inject(inject_id=i, title=title, description="") for i, title in enumerate(titles, 1)]


def test_inject_points_parses_title() -> None:
    assert inject_points("Inject 00 - Welcome (100 pts)") == 100
    assert inject_points("Inject 03 - Graylog (300 pts)") == 300


def test_inject_points_none_when_absent() -> None:
    assert inject_points("Untitled inject") is None
    assert inject_points("") is None


def test_inject_points_tolerant_formats() -> None:
    assert inject_points("X (100 Pts)") == 100  # case
    assert inject_points("X (1,000 points)") == 1000  # comma + "points"
    assert inject_points("X (250pts)") == 250  # no space


def test_inject_max_sums_titles() -> None:
    assert inject_max_from_titles(_injects("A (100 pts)", "B (155 pts)")) == Decimal("255")


def test_inject_max_none_if_any_title_unparsed() -> None:
    # An unparsed inject still contributes graded points, so a partial sum would under-count.
    assert inject_max_from_titles(_injects("A (100 pts)", "C (no value)")) is None


def test_inject_max_none_without_any_values() -> None:
    assert inject_max_from_titles(_injects("A", "B")) is None


def _detail(team: Team, name: str, points: str, uptime: str) -> None:
    ServiceDetail.objects.create(team=team, service_name=name, points=Decimal(points), uptime=Decimal(uptime))


def test_service_max_uses_highest_uptime_per_service() -> None:
    t1 = Team.objects.create(team_number=1, team_name="T1")
    t2 = Team.objects.create(team_number=2, team_name="T2")
    # web: best uptime is t2 at 0.9 -> 1004/0.9 = 1115.56 -> 1116; a noisy low-uptime sample is ignored
    _detail(t1, "web", "10", "0.01")  # 10/0.01 = 1000 (noisy, lower uptime -> ignored)
    _detail(t2, "web", "1004", "0.9")  # 1004/0.9 = 1115.56
    # ssh: single sample 558/0.5 = 1116
    _detail(t1, "ssh", "558", "0.5")
    result = service_max_from_details()
    assert result == Decimal("2232")  # 1116 + 1116


def test_service_max_ignores_zero_point_best_sample() -> None:
    # The highest-uptime team scored 0 (port up, content/auth check failed); it must not
    # "cover" the service with a 0 contribution. The real scorer (lower uptime) sets the max.
    t1 = Team.objects.create(team_number=1, team_name="T1")
    t2 = Team.objects.create(team_number=2, team_name="T2")
    _detail(t1, "web", "0", "0.95")  # best uptime, but 0 points -> must be ignored
    _detail(t2, "web", "800", "0.80")  # real scorer -> 800/0.8 = 1000
    _detail(t1, "ssh", "558", "0.50")  # -> 1116
    assert service_max_from_details() == Decimal("2116")  # 1000 + 1116, not 1116


def test_service_max_none_without_uptime() -> None:
    t1 = Team.objects.create(team_number=1, team_name="T1")
    _detail(t1, "web", "1000", "0")  # uptime 0 -> excluded
    assert service_max_from_details() is None


def test_set_template_max_writes_when_present() -> None:
    ScoringTemplate.objects.create(
        service_weight=Decimal("40"), inject_weight=Decimal("40"), orange_weight=Decimal("20")
    )
    set_template_max("inject_max", Decimal("2180"))
    assert ScoringTemplate.objects.first().inject_max == Decimal("2180")


def test_set_template_max_skips_none() -> None:
    t = ScoringTemplate.objects.create(
        service_weight=Decimal("40"), inject_weight=Decimal("40"), orange_weight=Decimal("20"), inject_max=Decimal("99")
    )
    set_template_max("inject_max", None)
    t.refresh_from_db()
    assert t.inject_max == Decimal("99")  # unchanged


def test_set_template_max_skips_without_template() -> None:
    set_template_max("inject_max", Decimal("2180"))
    assert ScoringTemplate.objects.first() is None  # nothing created


def test_orange_max_sums_check_max_scores() -> None:
    OrangeCheck.objects.create(title="A", max_points=50)
    check = OrangeCheck.objects.create(title="B", max_points=120)
    OrangeCheckCriterion.objects.create(orange_check=check, label="Only", points=30)
    assert OrangeCheck.objects.total_max() == 80
    assert ScoringTemplate().orange_max == Decimal("80")


def test_orange_max_is_zero_without_checks() -> None:
    assert ScoringTemplate().orange_max == Decimal("0")


def test_orange_max_follows_every_change_to_the_checks() -> None:
    template = ScoringTemplate.objects.create()
    check = OrangeCheck.objects.create(title="A", max_points=50)
    assert ScoringTemplate.objects.get().orange_max == Decimal("50")
    OrangeCheck.objects.filter(pk=check.pk).update(max_points=70)
    OrangeCheck.objects.bulk_create([OrangeCheck(title="B", max_points=5)])
    assert ScoringTemplate.objects.get(pk=template.pk).orange_max == Decimal("75")
    OrangeCheck.objects.all().delete()
    assert ScoringTemplate.objects.get(pk=template.pk).orange_max == Decimal("0")


def test_config_form_shows_maxes_read_only() -> None:
    from scoring.forms import ScoringTemplateForm

    fields = set(ScoringTemplateForm().fields)
    assert fields == {"service_weight", "inject_weight", "orange_weight"}
