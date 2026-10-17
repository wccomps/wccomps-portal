from django.db import migrations


class Migration(migrations.Migration):
    """Drop registration_eventteamassignment.password_generated, which 0009 removed from the model state but kept
    for pods on the previous release during that rollout.

    The lock timeout makes the drop fail, and the pod retry, when a long query holds the table, instead of
    queueing every request behind the ALTER. Reversing adds the column back with its default, as 0009 left it,
    so a rollback reaches the keeping migrations; it stops working past core 0015, team 0008 and scoring 0038,
    whose drops reverse as no-ops.
    """

    dependencies = [
        ("registration", "0012_drop_event_reminder_columns"),
    ]

    operations = [
        migrations.RunSQL(
            [
                "SET LOCAL lock_timeout = '5s'",
                "ALTER TABLE registration_eventteamassignment DROP COLUMN IF EXISTS password_generated",
            ],
            [
                "SET LOCAL lock_timeout = '5s'",
                "ALTER TABLE registration_eventteamassignment "
                "ADD COLUMN IF NOT EXISTS password_generated varchar(100) NOT NULL DEFAULT ''",
            ],
        ),
    ]
