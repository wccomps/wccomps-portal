"""Derive the scoring raw-maximums from the competition's own data.

These maxima feed the reactive scaling in ``calculator.py`` (each component's modifier
comes from its weight and raw max). They used to be typed in by hand on the config page
and went stale between events; these helpers recompute them from the live inputs.

- ``service_max``: each service's points at full uptime, taken as ``points / uptime`` from
  the service's highest-uptime team (the numerically most reliable sample), summed over
  services. Quotient exposes no per-round point value, so ``points / uptime`` is how the
  full-uptime total is recovered. If a scored service has no reliable uptime sample the
  sum would under-count (its points still sit in the team's gross service total), so the
  whole derivation is skipped rather than written too low.
- ``inject_max``: the sum of each inject's point value, which Quotient carries only in the
  inject title (e.g. ``"Inject 00 - Welcome (100 pts)"``). Skipped if any title lacks a
  value, since those injects' graded points still count toward the inject total.
- ``orange_max``: the sum of every orange check's maximum score, refreshed by a signal when
  a check or criterion changes (there is no sync action to hang it on).
"""

import logging
import re
from decimal import Decimal
from typing import Literal

from orange_team.models import OrangeCheck

from scoring.models import ScoringTemplate, ServiceDetail

logger = logging.getLogger(__name__)

# The point value as organizers write it in a Quotient inject title: "(100 pts)", "(1,000 points)".
_INJECT_POINTS_RE = re.compile(r"\(\s*([\d,]+)\s*(?:pts?|points?)\s*\)", re.IGNORECASE)

# Below this uptime a single sample extrapolates too far via points/uptime to trust.
_MIN_RELIABLE_UPTIME = Decimal("0.1")

TemplateMaxField = Literal["service_max", "inject_max", "orange_max"]


def inject_points(title: str) -> int | None:
    """The point value embedded in an inject title, or None if it has none."""
    match = _INJECT_POINTS_RE.search(title or "")
    return int(match.group(1).replace(",", "")) if match else None


def inject_max_from_titles(titles: list[str]) -> Decimal | None:
    """Sum of the per-inject point values, or None if any title lacks one.

    All-or-nothing on purpose: an inject with no parseable max still contributes its
    graded points to the total, so a partial sum would silently under-count.
    """
    values = [(title, inject_points(title)) for title in titles]
    missing = [title for title, points in values if points is None]
    if missing:
        logger.warning("inject_max not derived: %d inject title(s) have no point value: %s", len(missing), missing)
        return None
    total = sum(points for _, points in values if points is not None)
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
