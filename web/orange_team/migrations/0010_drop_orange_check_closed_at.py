from django.db import migrations


class Migration(migrations.Migration):
    """Drop orange_check.closed_at, which 0008 removed from the model state but kept for pods on the previous
    release during that rollout.

    The lock timeout makes the drop fail, and the pod retry, when a long query holds the table, instead of
    queueing every request behind the ALTER. Reversing adds the column back as 0008 left it, so a rollback
    reaches the keeping migrations; it stops working past core 0015, team 0008 and scoring 0038, whose drops
    reverse as no-ops.
    """

    dependencies = [
        ("orange_team", "0009_point_limits"),
    ]

    operations = [
        migrations.RunSQL(
            [
                "SET LOCAL lock_timeout = '5s'",
                "ALTER TABLE orange_check DROP COLUMN IF EXISTS closed_at",
            ],
            [
                "SET LOCAL lock_timeout = '5s'",
                "ALTER TABLE orange_check ADD COLUMN IF NOT EXISTS closed_at timestamp with time zone NULL",
            ],
        ),
    ]
