"""Test support: restore migration-seeded rows after a flush."""

import importlib
from typing import Any

from django.apps import apps
from django.core.management.color import no_style
from django.db import connection
from scoring.models import AttackType

from ticketing.models import TicketCategory

SEEDED_CATEGORY_IDS = (1, 2, 3, 4, 5, 6)
ACTIVE_ATTACK_TYPE_NAMES = (
    "Default Credentials",
    "Credential Reuse",
    "Remotely Exploitable Service",
    "Web Based Application",
    "Anonymous Access",
    "Data Leakage",
    "Other",
)


def ensure_seeded_categories() -> None:
    """Recreate the categories from migration 0009 if they are missing.

    They exist only because of a data migration, and pytest-django flushes every table after a
    transactional test. Under xdist worksteal a worker can run ordinary tests after that flush.
    """
    if TicketCategory.objects.filter(pk__in=SEEDED_CATEGORY_IDS).count() == len(SEEDED_CATEGORY_IDS):
        return

    TicketCategory.objects.filter(pk__in=SEEDED_CATEGORY_IDS).delete()
    seed = importlib.import_module("ticketing.migrations.0009_seed_ticket_categories")
    seed.seed_categories(apps, None)

    # Seeds use explicit ids; move the sequence past them so later creates don't collide
    with connection.cursor() as cursor:
        for sql in connection.ops.sequence_reset_sql(no_style(), [TicketCategory]):
            cursor.execute(sql)


def ensure_seeded_attack_types() -> None:
    """Recreate the attack types from migrations 0013 and 0015 if they are missing."""
    if AttackType.objects.filter(name__in=ACTIVE_ATTACK_TYPE_NAMES, is_active=True).count() == len(
        ACTIVE_ATTACK_TYPE_NAMES
    ):
        return

    seed_0013 = importlib.import_module("scoring.migrations.0013_add_attack_type_and_contributors")
    seed_0015 = importlib.import_module("scoring.migrations.0015_update_attack_types")
    seed_0013.seed_attack_types(apps, None)
    seed_0015.update_attack_types(apps, None)


def restore_migration_seeds(app_config: Any, **_: Any) -> None:
    """post_migrate receiver: restore migration-seeded tables after a TransactionTestCase flush."""
    if app_config.name == "ticketing":
        ensure_seeded_categories()
    elif app_config.name == "scoring":
        ensure_seeded_attack_types()
