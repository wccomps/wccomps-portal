from decimal import Decimal

import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):
    """Forget ScoringTemplate.orange_max: the scoring now sums the orange checks' maximums when it needs them.

    The column stays for pods still on the previous release during a rolling deploy, with a database default so new
    code can insert a template without it. A later release drops it.
    """

    dependencies = [
        ("scoring", "0047_redteamscore_access_level"),
    ]

    operations = [
        migrations.AlterField(
            model_name="scoringtemplate",
            name="orange_max",
            field=models.DecimalField(
                db_default=Decimal("0"),
                decimal_places=2,
                default=Decimal("160"),
                help_text="Maximum possible raw orange team check points",
                max_digits=10,
                validators=[django.core.validators.MinValueValidator(Decimal("0"))],
            ),
        ),
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.RemoveField(model_name="scoringtemplate", name="orange_max"),
            ],
        ),
    ]
