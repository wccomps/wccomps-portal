from django.db import migrations, models

import core.validators


class Migration(migrations.Migration):
    dependencies = [
        ("registration", "0016_remove_teamregistration_edit_token"),
    ]

    operations = [
        migrations.AddField(
            model_name="event",
            name="proxy_team_domain",
            field=models.CharField(
                "proxy team domain",
                max_length=254,
                blank=True,
                default="",
                validators=[core.validators.validate_team_dns_domain],
                help_text="Exact team domain to send to aweful-proxy only if Quotient lists no DNS service.",
            ),
        ),
    ]
