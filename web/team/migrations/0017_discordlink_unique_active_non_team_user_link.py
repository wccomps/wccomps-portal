from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("team", "0016_drop_discordlink_helper_columns"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="discordlink",
            constraint=models.UniqueConstraint(
                condition=models.Q(("is_active", True), ("team__isnull", True)),
                fields=("user",),
                name="team_unique_active_non_team_user_link",
            ),
        ),
    ]
