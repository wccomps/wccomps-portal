from django.db import migrations, models


class Migration(migrations.Migration):
    """Forget DiscordLink.helper_activated_at, helper_deactivated_at and helper_removal_reason, which were only
    ever written. The columns stay for pods still on the previous release during a rolling deploy, the NOT NULL
    one with a database default so new code can insert links without it. test_rollout_leftovers_dropped.py lists
    them until they are dropped."""

    dependencies = [
        ("team", "0013_school_info_one_team_per_school"),
    ]

    operations = [
        migrations.AlterField(
            model_name="discordlink",
            name="helper_removal_reason",
            field=models.TextField(blank=True, db_default="", help_text="Reason for helper access removal"),
        ),
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.RemoveField(model_name="discordlink", name="helper_activated_at"),
                migrations.RemoveField(model_name="discordlink", name="helper_deactivated_at"),
                migrations.RemoveField(model_name="discordlink", name="helper_removal_reason"),
            ],
        ),
    ]
