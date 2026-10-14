from django.db import migrations


class Migration(migrations.Migration):
    """Forget OrangeCheck.closed_at, which nothing set or read. The nullable column stays for pods still on the
    previous release during a rolling deploy; a later release drops it."""

    dependencies = [
        ("orange_team", "0007_remove_orangecheck_status"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.RemoveField(model_name="orangecheck", name="closed_at"),
            ],
        ),
    ]
