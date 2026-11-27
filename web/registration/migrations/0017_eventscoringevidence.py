import django.core.serializers.json
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("registration", "0016_remove_teamregistration_edit_token")]

    operations = [
        migrations.AddField(
            model_name="eventarchive",
            name="close_out_data",
            field=models.BinaryField(editable=False, null=True),
        ),
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
