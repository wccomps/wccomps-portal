import django.core.serializers.json
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("registration", "0016_remove_teamregistration_edit_token")]

    operations = [
        migrations.CreateModel(
            name="EventScoringEvidence",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("data", models.JSONField(encoder=django.core.serializers.json.DjangoJSONEncoder)),
                (
                    "event",
                    models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, to="registration.event"),
                ),
            ],
        ),
    ]
