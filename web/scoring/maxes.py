"""Derive ScoringTemplate.service_max and inject_max from Quotient data."""

import logging
from collections.abc import Sequence
from decimal import Decimal
from typing import Literal

from quotient.client import Inject, TeamServiceExport

from scoring.models import ScoringTemplate

logger = logging.getLogger(__name__)
TemplateMaxField = Literal["service_max", "inject_max"]


def inject_max_from_titles(injects: Sequence[Inject]) -> Decimal | None:
    """Sum of per-inject point values from a sequence of Inject objects, or None if any title lacks one.

    All-or-nothing on purpose: an inject with no parseable max still contributes its
    graded points to the total, so a partial sum would silently under-count.
    """
    missing = [i.title for i in injects if i.max_points is None]
    if missing:
        logger.warning("inject_max not derived: %d inject title(s) have no point value: %s", len(missing), missing)
        return None
    total = sum(i.max_points for i in injects if i.max_points is not None)
    return Decimal(total) if total > 0 else None


def service_max_from_export(exports: Sequence[TeamServiceExport]) -> Decimal | None:
    """Each service's highest max_points across these teams, summed; None unless every entry has one.

    The highest, since a team Quotient checked in fewer rounds reports less for the same service.
    """
    best: dict[str, int] = {}
    for team in exports:
        for svc in team.services:
            if svc.max_points is None:
                logger.warning("service_max not derived: Quotient's export has no max_points for %r", svc.service_name)
                return None
            best[svc.service_name] = max(best.get(svc.service_name, 0), svc.max_points)
    return Decimal(sum(best.values()))


def set_template_max(field: TemplateMaxField, value: Decimal | None) -> bool:
    """Set one raw-max field on the ScoringTemplate when a value was derived, and say whether it was stored.

    No-op when the value is missing (never clobbers a good max with a stale one) or when
    no template exists yet (the config page owns creating it).
    """
    if value is None:
        return False
    template = ScoringTemplate.objects.first()
    if template is None:
        return False
    setattr(template, field, value)
    template.updated_by = None  # a derived write, not a human edit — keep the audit trail honest
    template.save(update_fields=[field, "updated_by", "updated_at"])
    return True
