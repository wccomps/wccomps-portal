from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("scoring", "0042_remove_servicescore_point_adjustments"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="redteamscore",
            name="contributors",
        ),
    ]
