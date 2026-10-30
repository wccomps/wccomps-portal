from django.db import migrations, models


def _to_level(apps, schema_editor):
    finding = apps.get_model("scoring", "RedTeamScore")
    finding.objects.filter(root_access=True).update(access_level="root")
    finding.objects.filter(root_access=False, user_access=True).update(access_level="user")


def _to_booleans(apps, schema_editor):
    finding = apps.get_model("scoring", "RedTeamScore")
    finding.objects.update(root_access=False, user_access=False)
    finding.objects.filter(access_level="root").update(root_access=True)
    finding.objects.filter(access_level="user").update(user_access=True)


class Migration(migrations.Migration):
    """Fold RedTeamScore.root_access and user_access into one access_level.

    The two columns stay for pods still on the previous release during a rolling deploy, with a database default
    so new code can insert findings without them; access_level has one so old code can insert without it.
    The copy runs once, here: a finding the previous release writes during the rollout keeps its access only in
    the old columns, with access_level 'none'.
    """

    dependencies = [
        ("scoring", "0046_red_finding_outcome_help_and_ip_template"),
    ]

    operations = [
        migrations.AddField(
            model_name="redteamscore",
            name="access_level",
            field=models.CharField(
                choices=[("none", "No access"), ("user", "User access"), ("root", "Root access")],
                db_default="none",
                default="none",
                help_text="Highest access obtained",
                max_length=4,
            ),
        ),
        migrations.AlterField(
            model_name="redteamscore",
            name="root_access",
            field=models.BooleanField(db_default=False, default=False),
        ),
        migrations.AlterField(
            model_name="redteamscore",
            name="user_access",
            field=models.BooleanField(db_default=False, default=False),
        ),
        migrations.RunPython(_to_level, _to_booleans),
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.RemoveField(model_name="redteamscore", name="root_access"),
                migrations.RemoveField(model_name="redteamscore", name="user_access"),
            ],
        ),
    ]
