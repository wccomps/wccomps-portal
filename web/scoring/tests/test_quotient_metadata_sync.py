"""Metadata sync: always a fresh fetch, and a failed one keeps the last good metadata."""

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from scoring.models import QuotientMetadataCache, ScoringTemplate
from scoring.quotient_sync import sync_quotient_metadata

pytestmark = pytest.mark.django_db


def test_a_failed_sync_keeps_the_last_metadata():
    """One Quotient blip mid-competition used to empty the red-team and incident dropdowns for 15 minutes."""
    QuotientMetadataCache.objects.create(boxes=[{"name": "web", "ip": "10.0.0.5"}], services=[{"name": "web:http"}])

    with patch("scoring.quotient_sync.QuotientClient") as client:
        client.return_value.get_infrastructure.return_value = None
        with pytest.raises(ValueError, match="Failed to retrieve infrastructure"):
            sync_quotient_metadata()

    client.return_value.get_infrastructure.assert_called_once_with(force_refresh=True)
    assert QuotientMetadataCache.objects.get().boxes == [{"name": "web", "ip": "10.0.0.5"}]


def test_metadata_sync_derives_inject_max():
    ScoringTemplate.objects.create(
        service_weight=Decimal("40"), inject_weight=Decimal("40"), orange_weight=Decimal("20"), inject_max=Decimal("1")
    )
    infra = MagicMock(boxes=[], event_name="Event", team_count=5)
    injects = [MagicMock(title="Inject 00 (100 pts)"), MagicMock(title="Inject 01 (155 pts)")]

    with patch("scoring.quotient_sync.QuotientClient") as client:
        client.return_value.get_infrastructure.return_value = infra
        client.return_value.get_injects.return_value = injects
        sync_quotient_metadata()

    assert ScoringTemplate.objects.first().inject_max == Decimal("255")
