import django.core.serializers.json
import django.db.models.deletion
from django.db import migrations, models
from django.db.models import Exists, OuterRef


def classify_retained_archives(apps, schema_editor):
    archive = apps.get_model("registration", "EventArchive")
    audit = apps.get_model("core", "AuditLog")
    db = schema_editor.connection.alias
    kinds = {"event_finalized": "finalize", "event_closed_out": "close_out"}
    # Historical writers saved the ZIP before recording the event audit in the same transaction.
    writes = audit.objects.using(db).filter(
        target_entity="event", target_id=OuterRef("event_id"), created_at__gte=OuterRef("updated_at"),
    )
    retained = archive.objects.using(db).filter(data__isnull=False)
    for action, kind in kinds.items():
        retained.filter(
            Exists(writes.filter(action=action)),
            ~Exists(writes.filter(action__in=(*kinds, "event_cleared")).exclude(action=action)),
        ).update(retained_kind=kind)


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0027_queuedtask_sync_quotient_metadata"),
        ("registration", "0016_remove_teamregistration_edit_token"),
    ]

    operations = [
        migrations.AlterField(
            model_name="eventarchive",
            name="data",
            field=models.BinaryField(editable=False, null=True),
        ),
        migrations.AddField(
            model_name="eventarchive",
            name="close_out_data",
            field=models.BinaryField(editable=False, null=True),
        ),
        migrations.AddField(
            model_name="eventarchive",
            name="retained_kind",
            field=models.CharField(max_length=9, blank=True, default="", editable=False),
        ),
        migrations.RunPython(classify_retained_archives, migrations.RunPython.noop),
        migrations.CreateModel(
            name="EventScoringEvidence",
            fields=[
                ("data", models.JSONField(encoder=django.core.serializers.json.DjangoJSONEncoder)),
                (
                    "event",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT, primary_key=True, serialize=False,
                        to="registration.event",
                    ),
                ),
            ],
        ),
    ]
