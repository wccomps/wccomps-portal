import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("registration", "0016_remove_teamregistration_edit_token"),
        ("scoring", "0050_metadata_jsonb_storable"),
        ("team", "0018_team_channels_need_clearing"),
    ]

    operations = [
        migrations.CreateModel(
            name="ServiceCorrectionBatch",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("service_name", models.CharField(max_length=100)),
                ("ruling", models.SlugField(max_length=100)),
                ("manifest_sha256", models.CharField(max_length=64)),
                ("baseline_sha256", models.CharField(max_length=64)),
                ("service_modifier", models.CharField(max_length=64)),
                ("sync_conflict", models.BooleanField(default=False)),
                ("portal_baseline_sha256", models.CharField(max_length=64)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("revoked_at", models.DateTimeField(null=True)),
                ("event", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to="registration.event")),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("event", "service_name", "ruling"),
                        name="unique_service_correction_ruling",
                    ),
                    models.UniqueConstraint(
                        condition=models.Q(revoked_at__isnull=True),
                        fields=("event",),
                        name="one_active_service_correction_per_event",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="ServiceCorrection",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("quotient_team_id", models.PositiveBigIntegerField()),
                ("service_credit", models.PositiveIntegerField()),
                ("sla_credit", models.PositiveIntegerField()),
                ("team", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to="team.team")),
                (
                    "batch",
                    models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to="scoring.servicecorrectionbatch"),
                ),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(fields=("batch", "team"), name="one_service_correction_per_team"),
                    models.UniqueConstraint(
                        fields=("batch", "quotient_team_id"),
                        name="one_service_correction_per_quotient_id",
                    ),
                ],
            },
        ),
    ]
