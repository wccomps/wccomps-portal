from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from scoring.calculator import _get_modifiers
from scoring.models import ScoringTemplate

pytestmark = pytest.mark.django_db


def test_no_stored_modifier_fields() -> None:
    names = {f.name for f in ScoringTemplate._meta.get_fields()}
    assert "service_modifier" not in names
    assert "inject_modifier" not in names
    assert "orange_modifier" not in names


def test_modifiers_derived_from_weights_and_maxes() -> None:
    # Equal weight share and equal max → equal modifiers; the max sets the scale.
    t = ScoringTemplate.objects.create(
        service_weight=Decimal("40"),
        inject_weight=Decimal("40"),
        orange_weight=Decimal("20"),
        service_max=Decimal("1000"),
        inject_max=Decimal("1000"),
        orange_max=Decimal("500"),
    )
    service_mod, inject_mod, orange_mod = _get_modifiers(t)
    # Each category's scaled maximum is proportional to its weight.
    assert service_mod * t.service_max == inject_mod * t.inject_max  # equal weight -> equal share
    assert (service_mod * t.service_max) / (orange_mod * t.orange_max) == Decimal("2")  # 40 vs 20
    # Editing a max re-derives the scale (no stored modifier needed).
    t.inject_max = Decimal("2000")
    t.save()
    service_mod_after, _i, _o = _get_modifiers(t)
    assert service_mod_after != service_mod


def test_clean_requires_weights_sum_100() -> None:
    t = ScoringTemplate(service_weight=Decimal("40"), inject_weight=Decimal("40"), orange_weight=Decimal("30"))
    with pytest.raises(ValidationError):
        t.full_clean()
