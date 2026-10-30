from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("team", "0017_discordlink_unique_active_non_team_user_link"),
    ]

    operations = [
        migrations.AddField(
            model_name="team",
            name="channels_need_clearing",
            field=models.BooleanField(
                default=False,
                help_text="Its Discord role and category are to be removed: none is reused or granted meanwhile",
            ),
        ),
    ]
