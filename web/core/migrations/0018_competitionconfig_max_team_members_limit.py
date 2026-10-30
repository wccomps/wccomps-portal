import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0017_clear_superuser_and_staff_flags"),
    ]

    operations = [
        migrations.AlterField(
            model_name="competitionconfig",
            name="max_team_members",
            field=models.IntegerField(
                default=10,
                help_text="Maximum members per team",
                validators=[
                    django.core.validators.MinValueValidator(1),
                    django.core.validators.MaxValueValidator(20),
                ],
            ),
        ),
    ]
