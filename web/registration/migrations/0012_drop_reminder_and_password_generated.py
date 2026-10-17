from django.db import migrations


class Migration(migrations.Migration):
    """Drop the columns 0009 removed from the model state but kept for pods on the previous release during that
    rollout: the event reminder settings and the assignment's password_generated. Reversing adds the columns back, empty and as
    those migrations left them, so the earlier migrations can be unapplied."""

    dependencies = [
        ("registration", "0011_event_clash_message"),
    ]

    operations = [
        migrations.RunSQL(
            "ALTER TABLE registration_event DROP COLUMN IF EXISTS reminder_days, "
            "DROP COLUMN IF EXISTS last_reminder_sent",
            "ALTER TABLE registration_event ADD COLUMN IF NOT EXISTS reminder_days jsonb NOT NULL DEFAULT '[]'::jsonb, "
            "ADD COLUMN IF NOT EXISTS last_reminder_sent timestamp with time zone NULL",
        ),
        migrations.RunSQL(
            "ALTER TABLE registration_eventteamassignment DROP COLUMN IF EXISTS password_generated",
            "ALTER TABLE registration_eventteamassignment "
            "ADD COLUMN IF NOT EXISTS password_generated varchar(100) NOT NULL DEFAULT ''",
        ),
    ]
