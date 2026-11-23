import csv
import hashlib
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from io import BytesIO, StringIO
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

import psycopg2
import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.template.loader import render_to_string
from django.utils import timezone
from quotient.client import TeamUptime, _service_export
from registration.services import close_out_event, close_out_problem, set_team_school
from registration.stage import event_stage

from core.models import AuditLog, CompetitionConfig
from core.services.competition import run_competition
from scoring.calculator import calculate_team_score_detailed, compute_standings
from scoring.models import ScoringTemplate, ServiceCorrection, ServiceCorrectionBatch, ServiceDetail
from scoring.quotient_sync import sync_service_scores
from scoring.service_corrections import MANIFEST_HEADER, ServiceCorrectionLine, source_digest, source_rows
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
    service_points, manifest_row, full_void = getattr(request, "param", (100, b"742,4,5,9\n", False))
    exports = _service_export(
        [
            {
                "team_id": 742,
                "team_name": "team07",
                "gross_points": service_points,
                "total_sla_penalty": 5,
                "services": [
                    {"service_name": SERVICE, "service_points": service_points, "sla_violations": 1, "sla_penalty": 5},
                ],
            },
        ]
    )
    uptimes = [TeamUptime("team07", 7, {SERVICE: 0.5})]
    content = MANIFEST_HEADER + manifest_row
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
        if full_void:
            supplied.append("--full-void")
        output = StringIO()
        call_command("service_correction", "verify", *supplied, stdout=output)
        receipt = dict(part.split("=", 1) for part in output.getvalue().split()[1:])
        assert receipt["full_void"] == str(full_void).lower()
        baseline = receipt["baseline_sha256"]
        assert (baseline != source_digest(source_rows(SERVICE, exports, uptimes))) == full_void
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
    assert result.scorecard["score"]["service_corrections"] == [
        {
            "service_name": SERVICE,
            "ruling": RULING,
            "service_credit": 4,
            "sla_credit": 5,
            "service_modifier": {"$d": "1.0"},
        },
    ]
    frozen = context_from_snapshot(result.scorecard)
    assert frozen["score"]["service_correction"] == ServiceCorrectionLine(SERVICE, RULING, 4, 5, Decimal(1), False)
    rendered = render_to_string("scoring/_service_correction_lines.html", frozen)
    assert RULING in rendered
    assert "1.00" in rendered
    assert "full void," not in rendered
    service_detail = frozen["stats"]["service_stats"][0]
    assert (service_detail["points"], service_detail["sla_violations"]) == (Decimal(100), 1)
    with ZipFile(BytesIO(event.archive.data)) as archive:
        rows = csv.DictReader(StringIO(archive.read("final_scores.csv").decode()))
        row = next(row for row in rows if int(row["Team Number"]) == team.team_number)
        assert (row["Raw Service Credit"], row["SLA Credit"]) == ("4", "5")
    assert next(s for s in compute_standings() if s.team.pk == team.pk).service_correction is None


@pytest.mark.parametrize("correction", [(100, b"742,-100,5,-95\n", True)], indirect=True)
def test_full_void_preserves_import_and_can_be_revoked(correction):
    team, event, common, apply, _, _ = correction
    ScoringTemplate.objects.create(service_max=3388)
    args = [*apply, "--expect-composite", "-95"]
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
    assert (receipt.details["full_void"], receipt.details["service_credit"]) == (True, -100)
    call_command("service_correction", "revoke", *common, "--actor", "operator")
    assert next(s for s in compute_standings() if s.team.pk == team.pk).total_score == 95


def test_full_void_rejects_credit_that_does_not_zero_both_source_components(correction):
    _, _, _, apply, _, _ = correction
    with pytest.raises(CommandError, match="A full void must remove all service points"):
        call_command("service_correction", "apply", *apply, "--full-void", "--expect-composite", "9")
    assert not ServiceCorrectionBatch.objects.exists()


@pytest.mark.parametrize(
    ("correction", "delta", "composite"),
    [
        pytest.param((-10, b"742,0,5,5\n", False), 0, "5", id="sla-only"),
        pytest.param((-10, b"742,4,5,9\n", False), 4, "9", id="small-credit"),
        pytest.param((-10, b"742,10,5,15\n", True), 10, "15", id="full-void"),
    ],
    indirect=["correction"],
)
def test_correction_accepts_zero_and_positive_deltas_on_negative_earned_points(correction, delta, composite):
    team, _, _, apply, _, _ = correction
    call_command("service_correction", "apply", *apply, "--expect-composite", composite)
    score = next(s for s in compute_standings() if s.team.pk == team.pk)
    assert score.service_points == -10 + delta
    assert score.sla_penalties == 0
    assert ServiceCorrection.objects.get().service_credit == delta


def test_correction_still_rejects_debits_over_earned_points(correction):
    _, _, _, apply, _, _ = correction
    manifest = Path(apply[apply.index("--manifest") + 1])
    content = MANIFEST_HEADER + b"742,-101,5,-96\n"
    manifest.write_bytes(content)
    apply[apply.index("--manifest-sha256") + 1] = hashlib.sha256(content).hexdigest()
    with pytest.raises(CommandError, match="Service debit exceeds points earned"):
        call_command("service_correction", "apply", *apply, "--expect-composite", "-96")
    assert not ServiceCorrectionBatch.objects.exists()


@pytest.mark.parametrize("correction", [(100, b"742,-100,5,-95\n", True)], indirect=True)
def test_full_void_stays_zero_after_sync_and_weight_changes_in_final_scores(correction):
    team, event, _, apply, importer, exports = correction
    template = ScoringTemplate.objects.create(service_max=400, inject_max=800)
    args = [*apply, "--expect-composite", "-195"]
    call_command("service_correction", "apply", *args)

    other_team, _ = Team.objects.update_or_create(team_number=8, defaults={"team_name": "Team 08", "is_active": True})
    set_team_school(other_team, "Example College", contact_email="coach@example.org")
    other_service = replace(exports[0].services[0], service_name="Example-DNS")
    other = replace(exports[0], team_number=8, team_name="team08", quotient_team_id=843, services=[other_service])
    importer.get_service_export.return_value = [*exports, other]
    importer.get_uptimes.return_value = [
        TeamUptime("team07", 7, {SERVICE: 0.5}),
        TeamUptime("team08", 8, {"Example-DNS": 0.25}),
    ]
    sync_service_scores()
    template.refresh_from_db()
    assert template.service_max == 600
    after_sync = calculate_team_score_detailed(team)
    assert after_sync["service_modifier"] == Decimal(4) / 3
    assert after_sync["service_raw"] == 100
    assert after_sync["total_score"] == 0

    template.service_weight = Decimal(60)
    template.inject_weight = Decimal(20)
    template.save(update_fields=["service_weight", "inject_weight"])
    output = StringIO()
    call_command("service_correction", "apply", *args, stdout=output)
    assert output.getvalue().startswith("already applied batch=")
    assert "raw_service=-100 sla_credit=5 full_void=true" in output.getvalue()
    standings = {s.team.pk: s for s in compute_standings()}
    adjusted = standings[team.pk]
    assert adjusted.service_points == adjusted.sla_penalties == adjusted.total_score == 0
    assert adjusted.rank is None
    assert standings[other_team.pk].service_points == 400
    assert standings[other_team.pk].rank == 1
    credit = ServiceCorrection.objects.get()
    assert (credit.service_credit, credit.sla_credit) == (-100, 5)
    assert credit.batch.full_void
    assert Decimal(credit.batch.service_modifier) == 2
    receipt = AuditLog.objects.get(action="service_correction_applied", target_id=event.pk)
    assert (receipt.details["service_credit"], receipt.details["composite"]) == (-100, "-195.00")
    assert not close_out_problem(event_stage())

    close_out_event(event, "operator")
    result = event.results.get(team_number=team.team_number)
    assert result.total_score == 0
    frozen = context_from_snapshot(result.scorecard)
    assert frozen["score"]["service_correction"] == ServiceCorrectionLine(SERVICE, RULING, -100, 5, Decimal(2), True)
    assert frozen["scaling"]["service_modifier"] == Decimal(4)
    rendered = render_to_string("scoring/_service_correction_lines.html", frozen)
    assert "full void," in rendered
    assert "4.00" in rendered
    assert "2.00" not in rendered
    service_detail = frozen["stats"]["service_stats"][0]
    assert (service_detail["points"], service_detail["sla_violations"]) == (Decimal(100), 1)
    with ZipFile(BytesIO(event.archive.data)) as archive:
        rows = csv.DictReader(StringIO(archive.read("final_scores.csv").decode()))
        row = next(row for row in rows if int(row["Team Number"]) == team.team_number)
        assert row["Rank"] == ""
        assert Decimal(row["Total Score"]) == Decimal(row["Service Points"]) == Decimal(row["SLA Penalties"]) == 0
        assert (row["Raw Service Credit"], row["SLA Credit"]) == ("-100", "5")


def test_full_void_migration_preserves_legacy_exact_offsets_as_pinned(correction):
    team, event, _, _, _, _ = correction
    before = [("scoring", "0052_signed_service_corrections")]
    after = [("scoring", "0053_service_correction_full_void")]
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    executor.migrate(before)
    try:
        old_apps = executor.loader.project_state(before).apps
        old_batch = old_apps.get_model("scoring", "ServiceCorrectionBatch")
        old_credit = old_apps.get_model("scoring", "ServiceCorrection")
        batch = old_batch.objects.create(event_id=event.pk, service_name=SERVICE, ruling=RULING, service_modifier="1")
        old_credit.objects.create(batch=batch, team_id=team.pk, quotient_team_id=742, service_credit=-100, sla_credit=5)

        executor = MigrationExecutor(connection)
        executor.migrate(after)
        persisted = ServiceCorrectionBatch.objects.get(pk=batch.pk)
        assert (persisted.full_void, persisted.service_modifier) == (False, "1")
        assert ServiceCorrection.objects.get(batch=persisted).service_credit == -100
        # A pod on the previous schema can still insert without naming the new field.
        overlap = old_batch.objects.create(
            event_id=event.pk, service_name=SERVICE, ruling="revoked", service_modifier="1", revoked_at=timezone.now()
        )
        assert not ServiceCorrectionBatch.objects.get(pk=overlap.pk).full_void

        ScoringTemplate.objects.create(service_max=400, inject_max=800)
        score = next(s for s in compute_standings() if s.team.pk == team.pk)
        assert score.service_points == 100  # 100 observed at 2, -100 pinned at 1.
        assert score.sla_penalties == 0
    finally:
        MigrationExecutor(connection).migrate(latest)


@pytest.mark.parametrize(
    "correction",
    [
        pytest.param((100, b"742,-100,5,-95\n", False), id="verified-ordinary"),
        pytest.param((100, b"742,-100,5,-95\n", True), id="verified-full-void"),
    ],
    indirect=True,
)
def test_apply_rejects_verification_in_the_other_mode(correction):
    _, _, _, apply, _, _ = correction
    toggled = [arg for arg in apply if arg != "--full-void"] if "--full-void" in apply else [*apply, "--full-void"]
    with pytest.raises(CommandError, match="correction mode does not match the verified SHA256"):
        call_command("service_correction", "apply", *toggled, "--expect-composite", "-95")
    assert not ServiceCorrectionBatch.objects.exists()


@pytest.mark.parametrize("correction", [(100, b"742,-100,5,-95\n", True)], indirect=True)
def test_apply_refuses_a_concurrent_scaling_change(correction):
    _, _, _, apply, _, _ = correction
    ScoringTemplate.objects.create(service_max=400, inject_max=800)
    db = connection.settings_dict
    writer = psycopg2.connect(
        host=db["HOST"], port=db["PORT"], user=db["USER"], password=db["PASSWORD"], dbname=db["NAME"]
    )
    try:
        with writer, writer.cursor() as cursor:
            cursor.execute("UPDATE scoring_template SET service_weight = 60, inject_weight = 20")
            with pytest.raises(CommandError, match="Scoring configuration is being changed"):
                call_command("service_correction", "apply", *apply, "--expect-composite", "-195")
            assert not ServiceCorrectionBatch.objects.exists()
            assert not AuditLog.objects.filter(action="service_correction_applied").exists()
    finally:
        writer.close()
    with pytest.raises(CommandError, match="Expected score or rank check failed"):
        call_command("service_correction", "apply", *apply, "--expect-composite", "-195")
    call_command("service_correction", "apply", *apply, "--expect-composite", "-595")
