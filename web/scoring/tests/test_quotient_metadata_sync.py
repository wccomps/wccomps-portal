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
    from quotient.client import Inject

    ScoringTemplate.objects.create(
        service_weight=Decimal("40"), inject_weight=Decimal("40"), orange_weight=Decimal("20"), inject_max=Decimal("1")
    )
    infra = MagicMock(boxes=[])
    injects = [
        Inject(inject_id=1, title="Inject 00 (100 pts)", description=""),
        Inject(inject_id=2, title="Inject 01 (155 pts)", description=""),
    ]

    with patch("scoring.quotient_sync.QuotientClient") as client:
        client.return_value.get_infrastructure.return_value = infra
        client.return_value.get_injects.return_value = injects
        sync_quotient_metadata()

    assert ScoringTemplate.objects.first().inject_max == Decimal("255")


def _post_sync(admin_user, infrastructure):
    from django.test import Client
    from django.urls import reverse

    client = Client()
    client.force_login(admin_user)
    with patch("scoring.quotient_sync.QuotientClient") as quotient:
        quotient.return_value.get_infrastructure.return_value = infrastructure
        quotient.return_value.get_injects.return_value = []
        return client.post(reverse("admin_competition_action"), {"action": "sync_quotient"})


def test_a_sync_from_the_portal_records_who_ran_it(admin_user):
    from core.models import AuditLog

    response = _post_sync(admin_user, MagicMock(boxes=[]))

    assert response.status_code == 200, response.content
    assert QuotientMetadataCache.objects.get().synced_by == admin_user
    assert AuditLog.objects.get(action="quotient_metadata_synced").admin_user == admin_user.username


def test_an_unreachable_quotient_is_reported_not_answered_as_a_server_error(admin_user):
    """The request was fine: the page says why nothing synced."""
    from core.models import AuditLog

    response = _post_sync(admin_user, None)

    assert response.status_code == 200, response.content
    assert response.json() == {
        "success": False,
        "message": "Sync failed: Failed to retrieve infrastructure from Quotient",
    }
    assert not AuditLog.objects.filter(action="quotient_metadata_synced").exists()
