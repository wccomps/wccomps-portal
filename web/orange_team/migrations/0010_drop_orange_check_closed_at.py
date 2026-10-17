from django.db import migrations


class Migration(migrations.Migration):
    """Drop the orange_check.closed_at column 0008 removed from the model state but kept for pods on the
    previous release during that rollout. Reversing adds the columns back, empty and as
    those migrations left them, so the earlier migrations can be unapplied."""

    dependencies = [
        ("orange_team", "0009_point_limits"),
    ]

    operations = [
        migrations.RunSQL(
            "ALTER TABLE orange_check DROP COLUMN IF EXISTS closed_at",
            "ALTER TABLE orange_check ADD COLUMN IF NOT EXISTS closed_at timestamp with time zone NULL",
        ),
    ]
