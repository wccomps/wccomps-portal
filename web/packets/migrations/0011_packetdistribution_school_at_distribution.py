import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("packets", "0010_packet_stored_file"),
        ("registration", "0016_remove_teamregistration_edit_token"),
    ]

    operations = [
        migrations.AddField(
            model_name="packetdistribution",
            name="school_at_distribution",
            field=models.ForeignKey(
                editable=False, null=True, on_delete=django.db.models.deletion.PROTECT, to="registration.school",
            ),
        ),
    ]
