"""Derive ScoringTemplate.service_max, inject_max, and orange_max from Quotient and OrangeCheck data."""

import logging
from collections.abc import Sequence
from decimal import Decimal
from typing import Literal

from orange_team.models import OrangeCheck
from quotient.client import Inject

from scoring.models import ScoringTemplate, ServiceDetail

logger = logging.getLogger(__name__)

# Below this uptime a single sample extrapolates too far via points/uptime to trust.
_MIN_RELIABLE_UPTIME = Decimal("0.1")

TemplateMaxField = Literal["service_max", "inject_max", "orange_max"]


def inject_max_from_titles(injects: Sequence[Inject]) -> Decimal | None:
    """Sum of the per-inject point values, or None if any title lacks one.

    All-or-nothing on purpose: an inject with no parseable max still contributes its
    graded points to the total, so a partial sum would silently under-count.
    """
    missing = [i.title for i in injects if i.max_points is None]
    if missing:
        logger.warning("inject_max not derived: %d inject title(s) have no point value: %s", len(missing), missing)
        return None
    total = sum(i.max_points for i in injects if i.max_points is not None)
    return Decimal(total) if total > 0 else None


def service_max_from_details() -> Decimal | None:
    """Service points at full uptime summed over services, or None if coverage is incomplete."""
    best: dict[str, tuple[Decimal, Decimal]] = {}  # service_name -> (uptime, points) of the best sample
    scored: set[str] = set()  # services that carry points at all
    for detail in ServiceDetail.objects.only("service_name", "uptime", "points"):
        if detail.points <= 0:
            continue  # a 0-point sample must not "cover" a service — it would contribute 0 to the sum
        scored.add(detail.service_name)
        if detail.uptime >= _MIN_RELIABLE_UPTIME:
            current = best.get(detail.service_name)
            if current is None or detail.uptime > current[0]:
                best[detail.service_name] = (detail.uptime, detail.points)
    uncovered = scored - set(best)
    if uncovered:
        logger.warning(
            "service_max not derived: %d scored service(s) lack a reliable uptime sample: %s",
            len(uncovered),
            sorted(uncovered),
        )
        return None
    if not best:
        return None
    total = sum((points / uptime for uptime, points in best.values()), Decimal("0")).quantize(Decimal("1"))
    return total if total > 0 else None


def orange_max_from_checks() -> Decimal | None:
    """Sum of every orange check's maximum score (criteria sum, else its max_points)."""
    total = sum(check.max_score for check in OrangeCheck.objects.all())
    return Decimal(total) if total > 0 else None


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
