"""Seeded ticket categories and attack types survive transactional-test flushes via post_migrate."""

import pytest
from django.core.management import call_command
from scoring.models import AttackType

from ticketing.models import TicketCategory
from ticketing.tests.seeded_categories import (
    ACTIVE_ATTACK_TYPE_NAMES,
    SEEDED_CATEGORY_IDS,
    ensure_seeded_attack_types,
    ensure_seeded_categories,
)

pytestmark = pytest.mark.django_db


def test_restores_categories_after_flush():
    """A transactional test's flush empties the table; the helper puts the seed rows back."""
    TicketCategory.objects.all().delete()

    ensure_seeded_categories()

    assert set(TicketCategory.objects.values_list("pk", flat=True)) == set(SEEDED_CATEGORY_IDS)
    assert TicketCategory.objects.get(pk=6).display_name == "Other / General Issue"


def test_new_category_after_reseed_does_not_collide():
    """Seeds use explicit ids, so the sequence must be moved past them."""
    TicketCategory.objects.all().delete()
    ensure_seeded_categories()

    created = TicketCategory.objects.create(display_name="Extra", points=0, sort_order=99)

    assert created.pk not in SEEDED_CATEGORY_IDS


def test_noop_when_categories_present():
    before = list(TicketCategory.objects.order_by("pk").values_list("pk", "display_name"))

    ensure_seeded_categories()

    assert list(TicketCategory.objects.order_by("pk").values_list("pk", "display_name")) == before


def test_restores_attack_types_after_flush():
    """AttackType seeds from migrations 0013 and 0015 (active and deactivated rows) are restored."""
    AttackType.objects.all().delete()

    ensure_seeded_attack_types()

    assert set(AttackType.objects.filter(is_active=True).values_list("name", flat=True)) == set(
        ACTIVE_ATTACK_TYPE_NAMES
    )
    assert AttackType.objects.filter(is_active=False).exists()


def test_flush_command_restores_seeded_tables_via_post_migrate(transactional_db):
    """TransactionTestCase teardown runs flush, whose post_migrate signal restores both seeded tables."""
    call_command("flush", verbosity=0, interactive=False, reset_sequences=False, inhibit_post_migrate=False)

    assert set(TicketCategory.objects.values_list("pk", flat=True)) == set(SEEDED_CATEGORY_IDS)
    assert set(AttackType.objects.filter(is_active=True).values_list("name", flat=True)) == set(
        ACTIVE_ATTACK_TYPE_NAMES
    )
    created = TicketCategory.objects.create(display_name="Post-flush", points=0, sort_order=99)
    assert created.pk not in SEEDED_CATEGORY_IDS
