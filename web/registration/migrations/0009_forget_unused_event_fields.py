from django.db import migrations, models


class Migration(migrations.Migration):
    """Forget Event.reminder_days, Event.last_reminder_sent and EventTeamAssignment.password_generated, which
    nothing read (no reminder was ever sent; no password was ever stored).

    The columns stay for pods still on the previous release during a rolling deploy, the NOT NULL ones with a
    database default so new code can insert rows without them. 0012 and 0013 drop them.
    """

    dependencies = [
        ("registration", "0008_event_reminder_days_optional"),
    ]

    operations = [
        migrations.AlterField(
            model_name="event",
            name="reminder_days",
            field=models.JSONField(blank=True, default=list, db_default=[]),
        ),
        migrations.AlterField(
            model_name="eventteamassignment",
            name="password_generated",
            field=models.CharField(blank=True, max_length=100, db_default=""),
        ),
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.RemoveField(model_name="event", name="reminder_days"),
                migrations.RemoveField(model_name="event", name="last_reminder_sent"),
                migrations.RemoveField(model_name="eventteamassignment", name="password_generated"),
            ],
        ),
    ]
