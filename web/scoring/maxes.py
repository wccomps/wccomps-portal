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
    """Sum of per-service maximum points across teams from a Quotient score export, or None if incomplete.

    Takes the highest max_points seen for each service across teams so a check disabled
    for one team does not under-count the service. All-or-nothing on purpose: if any
    service in the export has no positive max_points, a partial sum would under-count.
    """
    best: dict[str, int] = {}
    services: set[str] = set()
    for team in exports:
        for svc in team.services:
            services.add(svc.service_name)
            if svc.max_points > 0:
                current = best.get(svc.service_name)
                if current is None or svc.max_points > current:
                    best[svc.service_name] = svc.max_points
    uncovered = services - set(best)
    if uncovered:
        logger.warning(
            "service_max not derived: %d service(s) have no max_points in Quotient export: %s",
            len(uncovered),
            sorted(uncovered),
        )
        return None
    if not best:
        return None
    return Decimal(sum(best.values()))


def set_template_max(field: TemplateMaxField, value: Decimal | None) -> None:
    """Set one raw-max field on the ScoringTemplate when a value was derived.

    No-op when the value is missing (never clobbers a good max with a stale one) or when
    no template exists yet (the config page owns creating it).
    """
    if value is None:
        return
    template = ScoringTemplate.objects.first()
    if template is None:
        return
    setattr(template, field, value)
    template.updated_by = None  # a derived write, not a human edit — keep the audit trail honest
    template.save(update_fields=[field, "updated_by", "updated_at"])
