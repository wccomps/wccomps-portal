from django.db import migrations


class Migration(migrations.Migration):
    """Drop registration_event.reminder_days and last_reminder_sent, which 0009 removed from the model state but
    kept for pods on the previous release during that rollout.

    The lock timeout makes the drop fail, and the pod retry, when a long query holds the table, instead of
    queueing every request behind the ALTER. Reversing adds the columns back with their defaults, as 0009 left
    them, so a rollback reaches the keeping migrations; it stops working past core 0015, team 0008 and scoring
    0038, whose drops reverse as no-ops.
    """

    dependencies = [
        ("registration", "0011_event_clash_message"),
    ]

    operations = [
        migrations.RunSQL(
            [
                "SET LOCAL lock_timeout = '5s'",
                "ALTER TABLE registration_event DROP COLUMN IF EXISTS reminder_days, "
                "DROP COLUMN IF EXISTS last_reminder_sent",
            ],
            [
                "SET LOCAL lock_timeout = '5s'",
                "ALTER TABLE registration_event "
                "ADD COLUMN IF NOT EXISTS reminder_days jsonb NOT NULL DEFAULT '[]'::jsonb, "
                "ADD COLUMN IF NOT EXISTS last_reminder_sent timestamp with time zone NULL",
            ],
        ),
    ]
