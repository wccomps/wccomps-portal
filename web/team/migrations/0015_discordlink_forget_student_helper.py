from django.db import migrations, models


class Migration(migrations.Migration):
    """Forget DiscordLink.is_student_helper, helper_role_name and helper_role_id: student helpers are retired.

    The columns stay for pods still on the previous release during a rolling deploy, the NOT NULL ones with a
    database default so new code can insert links without them. test_rollout_leftovers_dropped.py lists them until
    they are dropped.
    """

    dependencies = [
        ("team", "0014_discordlink_forget_helper_history"),
    ]

    operations = [
        migrations.AlterField(
            model_name="discordlink",
            name="is_student_helper",
            field=models.BooleanField(
                db_default=False,
                db_index=True,
                default=False,
                help_text="Whether this person is currently a student helper",
            ),
        ),
        migrations.AlterField(
            model_name="discordlink",
            name="helper_role_name",
            field=models.CharField(
                blank=True, db_default="", help_text='Discord role name (e.g., "UCI Invitationals 2026")', max_length=100
            ),
        ),
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.RemoveField(model_name="discordlink", name="is_student_helper"),
                migrations.RemoveField(model_name="discordlink", name="helper_role_name"),
                migrations.RemoveField(model_name="discordlink", name="helper_role_id"),
            ],
        ),
    ]
