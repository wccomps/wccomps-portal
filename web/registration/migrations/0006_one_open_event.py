from django.db import migrations, models


def refuse_two_active_events(apps, schema_editor):
    """The constraint below allows one active event; stop clearly rather than fail on building its index."""
    Event = apps.get_model("registration", "Event")
    active = list(Event.objects.filter(is_active=True).order_by("pk").values_list("pk", "name"))
    if len(active) > 1:
        names = ", ".join(f"{name} (id {pk})" for pk, name in active)
        raise RuntimeError(f"More than one event is active: {names}. Deactivate all but one, then migrate again.")


class Migration(migrations.Migration):
    dependencies = [("registration", "0005_school")]

    operations = [
        migrations.RunPython(refuse_two_active_events, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="event",
            constraint=models.UniqueConstraint(
                condition=models.Q(("is_active", True)), fields=("is_active",), name="one_open_event"
            ),
        ),
    ]
