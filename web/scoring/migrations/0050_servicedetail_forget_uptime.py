from decimal import Decimal

from django.db import migrations, models


class Migration(migrations.Migration):
    """Forget ServiceDetail.uptime: nothing reads it.

    The column stays for pods still on the previous release during a rolling deploy, with a database default so new
    code can insert a service detail without it. A later release drops it.
    """

    dependencies = [
        ("scoring", "0049_screenshot_mime_type_no_default"),
    ]

    operations = [
        migrations.AlterField(
            model_name="servicedetail",
            name="uptime",
            field=models.DecimalField(
                db_default=Decimal("0"),
                decimal_places=4,
                default=Decimal("0"),
                help_text="Uptime percentage (0.0000 to 1.0000)",
                max_digits=6,
            ),
        ),
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.RemoveField(model_name="servicedetail", name="uptime"),
            ],
        ),
    ]
