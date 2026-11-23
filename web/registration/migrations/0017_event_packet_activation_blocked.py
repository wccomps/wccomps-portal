from django.db import migrations, models


def block_existing_events(apps, schema_editor):
    # Old starts/stops are not attributable to an event, and both scores and timer state can be empty after Stop.
    apps.get_model("registration", "Event").objects.using(schema_editor.connection.alias).update(
        packet_activation_blocked=True
    )


class Migration(migrations.Migration):
    dependencies = [("registration", "0016_remove_teamregistration_edit_token")]

    operations = [
        migrations.AddField(
            model_name="event",
            name="packet_activation_blocked",
            field=models.BooleanField(default=False, editable=False),
        ),
        migrations.RunPython(block_existing_events, migrations.RunPython.noop),
    ]
