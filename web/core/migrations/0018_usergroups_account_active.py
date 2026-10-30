from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0017_clear_superuser_and_staff_flags"),
    ]

    operations = [
        # Added without a default, so every existing row reads null: not yet asked of Authentik
        migrations.AddField(
            model_name="usergroups",
            name="account_active",
            field=models.BooleanField(
                null=True, help_text="Whether the Authentik account is enabled; null until Authentik is asked"
            ),
        ),
        migrations.AlterField(
            model_name="usergroups",
            name="account_active",
            field=models.BooleanField(
                null=True,
                default=True,
                help_text="Whether the Authentik account is enabled; null until Authentik is asked",
            ),
        ),
        migrations.AddField(
            model_name="usergroups",
            name="groups_updated_at",
            field=models.DateTimeField(
                blank=True, null=True, help_text="When Authentik was read for the stored groups and state"
            ),
        ),
        migrations.CreateModel(
            name="CredentialsReset",
            fields=[
                (
                    "authentik_id",
                    models.CharField(
                        help_text="Authentik user UUID (sub claim)", max_length=64, primary_key=True, serialize=False
                    ),
                ),
                ("reset_at", models.DateTimeField()),
            ],
        ),
    ]
