from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("scoring", "0043_alter_orangeteamscore_orange_check"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="redteamscore",
            name="contributors",
        ),
    ]
