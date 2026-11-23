from decimal import Decimal
from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.core.validators import validate_slug
from django.db import connection, transaction
from django.utils import timezone
from portal.required import plain_http_settings
from quotient.client import QuotientClient
from registration.locks import school_list_held
from registration.models import Event, EventResult

from core.models import AuditLog, CompetitionConfig
from core.services.competition import run_lock
from scoring.calculator import _template, compute_standings
from scoring.models import ServiceCorrection, ServiceCorrectionBatch
from scoring.service_corrections import (
    MANIFEST_LIMIT,
    Credit,
    SourceRow,
    expected_portal_digest,
    parse_manifest,
    portal_digest,
    service_modifier,
    source_digest,
    source_rows,
)


class Command(BaseCommand):
    help = "Verify or record an approved service correction for a played event; or revoke an existing batch."

    def add_arguments(self, parser: CommandParser) -> None:
        sub = parser.add_subparsers(dest="operation", required=True)
        for name in ("verify", "apply", "revoke"):
            action = sub.add_parser(name)
            action.add_argument("--event", type=int, required=True)
            action.add_argument("--service", required=True)
            action.add_argument("--ruling", required=True)
            action.add_argument("--manifest-sha256", required=True)
            if name != "revoke":
                action.add_argument("--manifest", type=Path, required=True)
                action.add_argument("--full-void", action="store_true")
            if name != "verify":
                action.add_argument("--actor", required=True)
            if name == "apply":
                action.add_argument("--baseline-sha256", required=True)
                action.add_argument("--expect-composite", type=Decimal, required=True)
                action.add_argument("--expect-service-max", type=Decimal)
                action.add_argument("--require-unchanged-ranks", action="store_true")

    def handle(  # noqa: PLR0913
        self,
        *,
        operation: str,
        event: int,
        service: str,
        ruling: str,
        manifest_sha256: str,
        manifest: Path | None = None,
        actor: str = "",
        baseline_sha256: str = "",
        expect_composite: Decimal = Decimal(0),
        require_unchanged_ranks: bool = False,
        full_void: bool = False,
        expect_service_max: Decimal | None = None,
        **options: object,
    ) -> None:
        if not 0 < len(service) <= 100 or not 0 < len(ruling) <= 100 or len(actor) > 255:
            raise CommandError("Invalid service, ruling, or audit actor.")
        if operation != "verify" and not actor.strip():
            raise CommandError("Invalid service, ruling, or audit actor.")
        try:
            validate_slug(ruling)
            if operation == "revoke":
                self.stdout.write(self._revoke(event, service, ruling, manifest_sha256, actor))
                return
            if manifest is None:
                raise CommandError("A private manifest is required.")
            with manifest.open("rb") as stream:
                credits = parse_manifest(stream.read(MANIFEST_LIMIT + 1), manifest_sha256)
            if problems := plain_http_settings():
                raise ValueError("; ".join(problems))
            client = QuotientClient()
            exports, uptimes = client.get_service_export(), client.get_uptimes()
            if exports is None or uptimes is None:
                raise ValueError("Cannot validate the baseline against Quotient's live export and uptimes.")
            sources = source_rows(service, exports, uptimes)
            if [row.quotient_team_id for row in sources] != [credit.quotient_team_id for credit in credits]:
                raise ValueError("Manifest IDs do not match the complete Quotient service cohort.")
            if any(c.sla_credit > s.service_sla_deduction for c, s in zip(credits, sources, strict=True)):
                raise ValueError("SLA credit exceeds the service's original deduction.")
            if any(
                c.service_credit < 0 and -c.service_credit > s.service_points
                for c, s in zip(credits, sources, strict=True)
            ):
                raise ValueError("Service debit exceeds points earned on the service.")
            if full_void and any(
                (c.service_credit, c.sla_credit) != (-s.service_points, s.service_sla_deduction)
                for c, s in zip(credits, sources, strict=True)
            ):
                raise ValueError("A full void must remove all service points and restore all service SLA deductions.")
            source_sha = source_digest(sources)
            if operation == "apply" and baseline_sha256 != source_sha:
                raise ValueError("Quotient baseline does not match the verified SHA256.")
            if operation == "verify":
                with transaction.atomic():
                    with connection.cursor() as cursor:
                        cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                    self._checked_teams(event, service, sources)
                self.stdout.write(self._receipt("verified", credits, manifest_sha256, source_sha, full_void=full_void))
                return
            self.stdout.write(
                self._apply(
                    event,
                    service,
                    ruling,
                    manifest_sha256,
                    actor,
                    credits,
                    sources,
                    source_sha,
                    expect_composite,
                    require_unchanged_ranks,
                    expect_service_max,
                    full_void=full_void,
                )
            )
        except (OSError, ValueError, ValidationError) as exc:
            raise CommandError(str(exc)) from exc

    @staticmethod
    def _checked_teams(event_id: int, service: str, sources: list[SourceRow]) -> dict[int, int]:
        event = Event.objects.filter(pk=event_id, is_active=True, is_finalized=False).first()
        config = CompetitionConfig.objects.filter(pk=1, applications_enabled=False).first()
        if event is None or config is None or config.schedule_will_start():
            raise ValueError("The target event must be open, unfinalized, stopped, and have no pending start.")
        teams = dict(event.team_assignments.filter(team__is_active=True).values_list("team__team_number", "team_id"))
        numbers = {row.team_number for row in sources}
        if not numbers <= teams.keys() or portal_digest(service) != expected_portal_digest(sources):
            raise ValueError("Quotient's team mapping or service baseline does not match the current portal import.")
        return teams

    @staticmethod
    def _receipt(status: str, credits: list[Credit], manifest_sha: str, source_sha: str, *, full_void: bool) -> str:
        return (
            f"{status}: teams={len(credits)} raw_service={sum(c.service_credit for c in credits)} "
            f"sla_credit={sum(c.sla_credit for c in credits)} "
            f"full_void={str(full_void).lower()} "
            f"manifest_sha256={manifest_sha} baseline_sha256={source_sha}"
        )

    def _apply(  # noqa: PLR0913
        self,
        event_id: int,
        service: str,
        ruling: str,
        manifest_sha: str,
        actor: str,
        credits: list[Credit],
        sources: list[SourceRow],
        source_sha: str,
        expect_composite: Decimal,
        unchanged_ranks: bool,
        expect_service_max: Decimal | None,
        *,
        full_void: bool,
    ) -> str:
        with school_list_held(), run_lock() as locked, transaction.atomic():
            if not locked:
                raise ValueError("A competition transition is in progress.")
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            CompetitionConfig.locked()
            teams = self._checked_teams(event_id, service, sources)
            if expect_service_max is not None and _template().service_max != expect_service_max:
                raise ValueError("The portal service maximum does not match the expected denominator.")
            batches = ServiceCorrectionBatch.objects.filter(event_id=event_id)
            if batch := batches.filter(service_name=service, ruling=ruling).first():
                same_input = (batch.manifest_sha256, batch.baseline_sha256, batch.full_void) == (
                    manifest_sha,
                    source_sha,
                    full_void,
                )
                if batch.revoked_at is not None or not same_input:
                    raise ValueError("This ruling already has a conflicting or revoked batch.")
                receipt = AuditLog.objects.get(
                    action="service_correction_applied",
                    target_entity="event",
                    target_id=event_id,
                    details__batch=batch.pk,
                ).details
                composite, moves = Decimal(receipt["composite"]), receipt["rank_changes"]
                if composite != expect_composite or (unchanged_ranks and moves):
                    raise ValueError("Application expectations do not match the recorded batch outcome.")
                return self._receipt(
                    f"already applied batch={batch.pk}", credits, manifest_sha, source_sha, full_void=full_void
                )
            if batches.filter(revoked_at__isnull=True).exists():
                raise ValueError("An active service correction already exists for this event.")
            before = {s.team.pk: s for s in compute_standings()}
            batch = ServiceCorrectionBatch.objects.create(
                event_id=event_id,
                service_name=service,
                ruling=ruling,
                manifest_sha256=manifest_sha,
                baseline_sha256=source_sha,
                portal_baseline_sha256=expected_portal_digest(sources),
                service_modifier=str(service_modifier()),
                full_void=full_void,
            )
            ServiceCorrection.objects.bulk_create(
                ServiceCorrection(
                    batch=batch,
                    team_id=teams[source.team_number],
                    quotient_team_id=credit.quotient_team_id,
                    service_credit=credit.service_credit,
                    sla_credit=credit.sla_credit,
                )
                for credit, source in zip(credits, sources, strict=True)
            )
            after = compute_standings()
            total_field = EventResult._meta.get_field("total_score")
            for standing in after:
                total_field.run_validators(standing.total_score)
            composite = sum((s.total_score - before[s.team.pk].total_score for s in after), Decimal(0))
            moves = sum(s.rank != before[s.team.pk].rank for s in after)
            if composite != expect_composite or (unchanged_ranks and moves):
                raise ValueError(f"Expected score or rank check failed: composite={composite}, rank_changes={moves}.")
            AuditLog.objects.create(
                action="service_correction_applied",
                admin_user=actor,
                target_entity="event",
                target_id=event_id,
                details={
                    "batch": batch.pk,
                    "ruling": ruling,
                    "service": service,
                    "teams": len(credits),
                    "manifest_sha256": manifest_sha,
                    "baseline_sha256": source_sha,
                    "full_void": full_void,
                    "service_credit": sum(c.service_credit for c in credits),
                    "sla_credit": sum(c.sla_credit for c in credits),
                    "composite": str(composite),
                    "rank_changes": moves,
                },
            )
            status = f"applied batch={batch.pk} event={event_id} composite={composite} rank_changes={moves}"
            return self._receipt(status, credits, manifest_sha, source_sha, full_void=full_void)

    @staticmethod
    def _revoke(event_id: int, service: str, ruling: str, manifest_sha: str, actor: str) -> str:
        with school_list_held(), run_lock() as locked, transaction.atomic():
            if not locked:
                raise ValueError("A competition transition is in progress.")
            config = CompetitionConfig.locked()
            if (
                config.applications_enabled
                or not Event.objects.filter(
                    pk=event_id,
                    is_active=True,
                    is_finalized=False,
                ).exists()
            ):
                raise ValueError("The target event must be open, unfinalized, and stopped.")
            batch = ServiceCorrectionBatch.objects.filter(
                event_id=event_id,
                service_name=service,
                ruling=ruling,
            ).first()
            if batch is None or batch.manifest_sha256 != manifest_sha:
                raise ValueError("No matching batch with that manifest SHA256.")
            if batch.revoked_at is not None:
                return f"already revoked batch={batch.pk} event={event_id}"
            if config.schedule_will_start():
                raise ValueError("Clear the pending competition start before revoking the correction.")
            batch.revoked_at = timezone.now()
            batch.save(update_fields=["revoked_at"])
            AuditLog.objects.create(
                action="service_correction_revoked",
                admin_user=actor,
                target_entity="event",
                target_id=event_id,
                details={"batch": batch.pk, "service": service, "ruling": ruling, "manifest_sha256": manifest_sha},
            )
            return f"revoked batch={batch.pk} event={event_id}"
