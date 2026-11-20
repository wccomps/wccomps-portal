from decimal import Decimal

import pytest
from orange_team.models import OrangeCheck

from scoring.models import ScoringTemplate


@pytest.fixture
def unit_modifiers(db):
    # Maxes proportional to weights -> every derived modifier is exactly 1 (raw == scaled).
    OrangeCheck.objects.create(title="Orange", max_points=20)
    return ScoringTemplate.objects.create(service_max=Decimal("40"), inject_max=Decimal("40"))
