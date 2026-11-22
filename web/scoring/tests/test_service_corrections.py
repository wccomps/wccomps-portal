import csv
import hashlib
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from io import BytesIO, StringIO
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone
from quotient.client import TeamUptime, _service_export
from registration.services import close_out_event, close_out_problem, set_team_school
from registration.stage import event_stage

from core.models import AuditLog, CompetitionConfig
from core.services.competition import run_competition
from scoring.calculator import compute_standings
from scoring.models import ScoringTemplate, ServiceCorrection, ServiceCorrectionBatch, ServiceDetail
from scoring.quotient_sync import sync_service_scores
from scoring.service_corrections import MANIFEST_HEADER
from scoring.snapshot import context_from_snapshot
from team.models import Team

pytestmark = pytest.mark.django_db(transaction=True)
SERVICE = "Example-HTTPS"
RULING = "certificate-expiry-v1"


@pytest.fixture
def correction(current_event, tmp_path, request):
    CompetitionConfig.objects.update_or_create(pk=1, defaults={"applications_enabled": False})
    team, _ = Team.objects.update_or_create(team_number=7, defaults={"team_name": "Team 07", "is_active": True})
    set_team_school(team, "Example University", contact_email="coach@example.edu")
    exports = _service_export(
        [
            {
                "team_id": 742,
                "team_name": "team07",
                "gross_points": 100,
                "total_sla_penalty": 5,
                "services": [
                    {"service_name": SERVICE, "service_points": 100, "sla_violations": 1, "sla_penalty": 5},
                ],
            },
        ]
    )
    uptimes = [TeamUptime("team07", 7, {SERVICE: 0.5})]
    content = MANIFEST_HEADER + getattr(request, "param", b"742,4,5,9\n")
    manifest = tmp_path / "approved.csv"
    manifest.write_bytes(content)
    with (
        patch("scoring.quotient_sync.QuotientClient") as importer,
        patch("scoring.management.commands.service_correction.QuotientClient") as verifier,
    ):
        for client in (importer, verifier):
            client.return_value.get_service_export.return_value = exports
            client.return_value.get_uptimes.return_value = uptimes
        sync_service_scores()
        common = [
            "--event",
            str(current_event.pk),
            "--service",
            SERVICE,
            "--ruling",
            RULING,
            "--manifest-sha256",
            hashlib.sha256(content).hexdigest(),
        ]
        supplied = [*common, "--manifest", str(manifest)]
        output = StringIO()
        call_command("service_correction", "verify", *supplied, stdout=output)
        baseline = output.getvalue().strip().rsplit("=", 1)[-1]
        apply = [*supplied, "--baseline-sha256", baseline, "--actor", "operator"]
        yield team, current_event, common, apply, importer.return_value, exports


def test_correction_is_audited_idempotent_sync_safe_and_revocable(correction):
    team, event, common, apply, importer, exports = correction
    before = {s.team.pk: s.total_score for s in compute_standings()}
    with pytest.raises(CommandError, match="Expected score or rank check failed"):
        call_command("service_correction", "apply", *apply, "--expect-composite", "10")
    assert not ServiceCorrectionBatch.objects.exists()
    assert not AuditLog.objects.filter(action="service_correction_applied").exists()

    for expected in ("applied", "already applied"):
        output = StringIO()
        call_command(
            "service_correction", "apply", *apply, "--expect-composite", "9", "--require-unchanged-ranks", stdout=output
        )
        assert output.getvalue().startswith(f"{expected} batch=")
    credit = ServiceCorrection.objects.get()
    assert (credit.team_id, credit.quotient_team_id) == (team.pk, 742)
    applied = AuditLog.objects.get(action="service_correction_applied", target_id=event.pk)
    assert (applied.admin_user, applied.details["composite"]) == ("operator", "9.00")
    with pytest.raises(CommandError, match="recorded batch outcome"):
        call_command("service_correction", "apply", *apply, "--expect-composite", "10")
    with pytest.raises(StopIteration) as stopped:
        next(run_competition(True, "operator"))
    assert "Revoke the active service correction" in stopped.value.value.error

    sync_service_scores()
    score = next(s for s in compute_standings() if s.team.pk == team.pk)
    assert score.total_score - before[team.pk] == Decimal(9)
    assert score.sla_penalties == 0
    importer.get_service_export.return_value = [replace(exports[0], gross_points=101)]
    with pytest.raises(ValueError, match="would change the baseline"):
        sync_service_scores()
    assert next(s for s in compute_standings() if s.team.pk == team.pk).total_score == score.total_score
    assert "Score sync conflicted" in close_out_problem(event_stage())
    importer.get_service_export.return_value = exports
    sync_service_scores()
    assert not close_out_problem(event_stage())
    ServiceDetail.objects.filter(team=team, service_name=SERVICE).update(points=99)
    assert "Reconcile before finalizing" in close_out_problem(event_stage())
    CompetitionConfig.objects.update(competition_start_time=timezone.now() + timedelta(hours=1))
    with pytest.raises(CommandError, match="Clear the pending competition start"):
        call_command("service_correction", "revoke", *common, "--actor", "operator")
    CompetitionConfig.objects.update(competition_start_time=None)

    for expected in ("revoked", "already revoked"):
        output = StringIO()
        call_command("service_correction", "revoke", *common, "--actor", "operator", stdout=output)
        assert output.getvalue().startswith(f"{expected} batch=")
    assert AuditLog.objects.get(action="service_correction_revoked", target_id=event.pk).admin_user == "operator"
    assert ServiceCorrection.objects.get().pk == credit.pk
    assert next(s for s in compute_standings() if s.team.pk == team.pk).total_score == before[team.pk]


def test_wrong_digest_and_ambiguous_mapping_do_not_apply(correction):
    _, _, _, apply, _, exports = correction
    manifest = Path(apply[apply.index("--manifest") + 1])
    approved = manifest.read_bytes()
    manifest.write_bytes(approved.replace(b",9", b",8"))
    with pytest.raises(CommandError, match="approved SHA256"):
        call_command("service_correction", "apply", *apply, "--expect-composite", "9")
    manifest.write_bytes(approved)
    exports.append(replace(exports[0], team_number=8, services=[]))
    with pytest.raises(CommandError, match="ambiguous Quotient team ID"):
        call_command("service_correction", "apply", *apply, "--expect-composite", "9")
    assert not ServiceCorrectionBatch.objects.exists()


def test_sync_can_revalue_raw_scores_without_revaluing_an_applied_credit(correction):
    team, _, _, apply, importer, exports = correction
    ScoringTemplate.objects.create(service_max=400, inject_max=800)
    call_command("service_correction", "apply", *apply, "--expect-composite", "13")
    other = replace(exports[0].services[0], service_name="Example-DNS")
    importer.get_service_export.return_value = [replace(exports[0], services=[*exports[0].services, other])]
    importer.get_uptimes.return_value = [TeamUptime("team07", 7, {SERVICE: 0.5, "Example-DNS": 0.25})]
    sync_service_scores()
    assert ServiceDetail.objects.filter(team=team, service_name="Example-DNS").exists()
    assert ScoringTemplate.objects.get().service_max == 600
    assert not close_out_problem(event_stage())
    changed = next(s for s in compute_standings() if s.team.pk == team.pk)
    assert changed.service_points == Decimal("141.33")  # 100 at 4/3, 4 at the pinned multiplier of 2.


def test_close_out_freezes_corrections_and_unaltered_service_history(correction):
    team, event, _, apply, _, _ = correction
    call_command("service_correction", "apply", *apply, "--expect-composite", "9")
    close_out_event(event, "operator")
    result = event.results.get(team_number=team.team_number)
    assert result.total_score == Decimal(104)
    frozen = context_from_snapshot(result.scorecard)
    assert frozen["score"]["service_corrections"] == [
        {
            "service_name": SERVICE,
            "ruling": RULING,
            "service_credit": 4,
            "sla_credit": 5,
            "service_modifier": Decimal(1),
        },
    ]
    service_detail = frozen["stats"]["service_stats"][0]
    assert (service_detail["points"], service_detail["sla_violations"]) == (Decimal(100), 1)
    with ZipFile(BytesIO(event.archive.data)) as archive:
        rows = csv.DictReader(StringIO(archive.read("final_scores.csv").decode()))
        row = next(row for row in rows if int(row["Team Number"]) == team.team_number)
        assert (row["Raw Service Credit"], row["SLA Credit"]) == ("4", "5")
    assert not next(s for s in compute_standings() if s.team.pk == team.pk).service_corrections


@pytest.mark.parametrize("correction", [b"742,-100,5,-95\n"], indirect=True)
def test_full_void_preserves_import_and_can_be_revoked(correction):
    team, event, common, apply, _, _ = correction
    ScoringTemplate.objects.create(service_max=3388)
    args = [*apply, "--full-void", "--expect-composite", "-95"]
    with pytest.raises(CommandError, match="expected denominator"):
        call_command("service_correction", "apply", *args, "--expect-service-max", "3389")
    assert not ServiceCorrectionBatch.objects.exists()
    call_command("service_correction", "apply", *args, "--expect-service-max", "3388")
    adjusted = next(s for s in compute_standings() if s.team.pk == team.pk)
    assert adjusted.service_points == adjusted.sla_penalties == adjusted.total_score == 0
    assert ScoringTemplate.objects.get().service_max == 3388
    detail = ServiceDetail.objects.get(team=team, service_name=SERVICE)
    assert (detail.points, detail.sla_violations) == (100, 1)
    receipt = AuditLog.objects.get(action="service_correction_applied", target_id=event.pk)
    assert receipt.details["service_credit"] == -100
    call_command("service_correction", "revoke", *common, "--actor", "operator")
    assert next(s for s in compute_standings() if s.team.pk == team.pk).total_score == 95


def test_full_void_rejects_credit_that_does_not_zero_both_source_components(correction):
    _, _, _, apply, _, _ = correction
    with pytest.raises(CommandError, match="A full void must remove all service points"):
        call_command("service_correction", "apply", *apply, "--full-void", "--expect-composite", "9")
    assert not ServiceCorrectionBatch.objects.exists()
