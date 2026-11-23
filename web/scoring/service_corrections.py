import hashlib
import json
import re
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from typing import TypedDict

from django.db import transaction
from quotient.client import TeamServiceExport, TeamUptime

from .models import ServiceCorrection, ServiceCorrectionBatch, ServiceDetail

MANIFEST_HEADER = b"team_id,check_points_delta,sla_points_restored,net_points_delta\n"
MANIFEST_LIMIT = 32 * 1024


@dataclass(frozen=True)
class Credit:
    quotient_team_id: int
    service_credit: int
    sla_credit: int


class CreditLine(TypedDict):
    service_name: str
    ruling: str
    service_credit: int
    sla_credit: int
    service_modifier: Decimal


def current_credits(team_ids: list[int]) -> dict[int, CreditLine]:
    rows = ServiceCorrection.objects.filter(
        batch__event__is_active=True, batch__revoked_at__isnull=True, team_id__in=team_ids
    ).values_list(
        "team_id",
        "batch__service_name",
        "batch__ruling",
        "service_credit",
        "sla_credit",
        "batch__service_modifier",
    )
    return {
        team_id: CreditLine(
            service_name=service,
            ruling=ruling,
            service_credit=service_credit,
            sla_credit=sla_credit,
            service_modifier=Decimal(modifier),
        )
        for team_id, service, ruling, service_credit, sla_credit, modifier in rows
    }


def parse_manifest(data: bytes, approved_sha256: str) -> list[Credit]:
    if len(data) > MANIFEST_LIMIT or hashlib.sha256(data).hexdigest() != approved_sha256:
        raise ValueError("Manifest does not match the approved SHA256.")
    if not data.startswith(MANIFEST_HEADER) or not data.endswith(b"\n"):
        raise ValueError("Expected the canonical correction CSV header and a final LF.")
    credits: list[Credit] = []
    for line in data[len(MANIFEST_HEADER) : -1].split(b"\n"):
        if not re.fullmatch(
            rb"[1-9][0-9]{0,8},(?:0|-?[1-9][0-9]{0,8}),(?:0|[1-9][0-9]{0,8}),(?:0|-?[1-9][0-9]{0,8})", line
        ):
            raise ValueError("Use canonical integers; only the service and net deltas may be negative.")
        team, service, sla, net = map(int, line.split(b","))
        if team <= (credits[-1].quotient_team_id if credits else 0) or net != service + sla:
            raise ValueError("Correction rows must be unique, ordered by Quotient team ID, and reconcile to net.")
        credits.append(Credit(team, service, sla))
    return credits


type PortalBaseline = tuple[int, int, int, int, int, int]


@dataclass(frozen=True)
class SourceRow:
    quotient_team_id: int
    # Team number, gross points, SLA deduction, service points, violation count, uptime × 10000.
    portal: PortalBaseline
    service_sla_deduction: int

    @property
    def team_number(self) -> int:
        return self.portal[0]

    @property
    def service_points(self) -> int:
        return self.portal[3]


def _digest(rows: object) -> str:
    return hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()


def source_rows(service_name: str, exports: list[TeamServiceExport], uptimes: list[TeamUptime]) -> list[SourceRow]:
    uptime_by_team: dict[int, TeamUptime] = {}
    ambiguous_uptime: set[int] = set()
    for uptime in uptimes:
        if uptime.team_number in uptime_by_team:
            ambiguous_uptime.add(uptime.team_number)
        uptime_by_team[uptime.team_number] = uptime
    rows: list[SourceRow] = []
    id_counts = Counter(row.quotient_team_id for row in exports)
    number_counts = Counter(row.team_number for row in exports)
    for row in exports:
        details = [detail for detail in row.services if detail.service_name == service_name]
        if not details:
            continue
        team_id, number = row.quotient_team_id, row.team_number
        team_uptime = uptime_by_team.get(number)
        if (
            len(details) != 1
            or team_id is None
            or team_id <= 0
            or id_counts[team_id] != 1
            or number <= 0
            or number_counts[number] != 1
            or number in ambiguous_uptime
            or team_uptime is None
            or service_name not in team_uptime.uptimes
        ):
            raise ValueError("Missing or ambiguous Quotient team ID, portal team number, or service export.")
        detail = details[0]
        fraction = Decimal(str(team_uptime.uptimes[service_name])).quantize(Decimal("0.0001"))
        rows.append(
            SourceRow(
                team_id,
                (
                    number,
                    row.gross_points,
                    row.total_sla_penalty,
                    detail.service_points,
                    detail.sla_violations,
                    int(fraction * 10000),
                ),
                detail.sla_penalty,
            )
        )
    return sorted(rows, key=lambda row: row.quotient_team_id)


def source_digest(rows: list[SourceRow]) -> str:
    return _digest([(row.quotient_team_id, *row.portal, row.service_sla_deduction) for row in rows])


def expected_portal_digest(rows: list[SourceRow]) -> str:
    return _digest(sorted(row.portal for row in rows))


def portal_digest(service_name: str) -> str:
    details = ServiceDetail.objects.filter(service_name=service_name).values_list(
        "team__team_number",
        "team__service_scores__service_points",
        "team__service_scores__sla_violations",
        "points",
        "sla_violations",
        "uptime",
    )
    rows = [
        (
            n,
            _integral(gross),
            _integral(-sla if sla is not None else None),
            _integral(points),
            count,
            _integral(uptime * 10000),
        )
        for n, gross, sla, points, count, uptime in details
    ]
    return _digest(sorted(rows))


def _integral(value: Decimal | None) -> int | str:
    # Fractional or missing values must not match the scorer's integers.
    return int(value) if value is not None and value == int(value) else str(value)


def service_modifier() -> Decimal:
    from .calculator import _get_modifiers, _template

    return _get_modifiers(_template())[0]


class CorrectionConflictError(ValueError):
    pass


@contextmanager
def guarded_service_sync(event_id: int | None) -> Iterator[None]:
    try:
        with transaction.atomic():
            yield
    except CorrectionConflictError:
        if event_id is not None:
            ServiceCorrectionBatch.objects.filter(event_id=event_id, revoked_at__isnull=True).update(sync_conflict=True)
        raise


def check_sync(event_id: int | None, exports: list[TeamServiceExport], uptimes: list[TeamUptime]) -> None:
    if event_id is None:
        return
    for batch in ServiceCorrectionBatch.objects.filter(event_id=event_id, revoked_at__isnull=True):
        try:
            incoming = source_digest(source_rows(batch.service_name, exports, uptimes))
        except ValueError as error:
            raise CorrectionConflictError(f"Cannot verify source for correction batch {batch.pk}.") from error
        if incoming != batch.baseline_sha256:
            raise CorrectionConflictError(f"Score sync would change the baseline of correction batch {batch.pk}.")


def correction_problem(event_id: int | None, *, check_sync_conflict: bool = True) -> str:
    if event_id is None:
        return ""
    for batch in ServiceCorrectionBatch.objects.filter(event_id=event_id, revoked_at__isnull=True):
        if check_sync_conflict and batch.sync_conflict:
            return f"Score sync conflicted with service correction batch {batch.pk}. Reconcile before finalizing."
        if portal_digest(batch.service_name) != batch.portal_baseline_sha256:
            return f"Imported scores do not match service correction batch {batch.pk}. Reconcile before finalizing."
    return ""
