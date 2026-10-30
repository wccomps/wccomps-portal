from django.db import migrations, models


def record_last_runs(apps, schema_editor):
    """The last start and stop, from the audit entries the pages read them from until now."""
    AuditLog = apps.get_model("core", "AuditLog")
    CompetitionConfig = apps.get_model("core", "CompetitionConfig")
    runs = AuditLog.objects.filter(action__in=("competition_started", "competition_stopped")).order_by(
        "-created_at", "-pk"
    )
    changes = {}
    if (last := runs.first()) is not None:
        changes["last_run_incomplete"] = bool(last.details.get("apps_failed") or last.details.get("accounts_failed"))
    start = runs.filter(action="competition_started").first()
    if start is not None and "accounts_ok" in start.details and "accounts_failed" in start.details:
        changes["last_start_accounts_ok"] = start.details["accounts_ok"]
        changes["last_start_accounts_tried"] = start.details["accounts_ok"] + start.details["accounts_failed"]
    if CompetitionConfig.objects.filter(pk=1, applications_enabled=True).exists():
        changes["started_this_window"] = True
    if changes:
        CompetitionConfig.objects.get_or_create(pk=1)
        CompetitionConfig.objects.filter(pk=1).update(**changes)


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0018_competitionconfig_max_team_members_limit"),
    ]

    operations = [
        migrations.AddField(
            model_name="competitionconfig",
            name="last_run_incomplete",
            field=models.BooleanField(
                default=False,
                help_text="The last start or stop left an app or team account unchanged, so it needs running again",
            ),
        ),
        migrations.AddField(
            model_name="competitionconfig",
            name="last_start_accounts_ok",
            field=models.PositiveIntegerField(blank=True, help_text="Team accounts the last start turned on", null=True),
        ),
        migrations.AddField(
            model_name="competitionconfig",
            name="last_start_accounts_tried",
            field=models.PositiveIntegerField(
                blank=True, help_text="Team accounts the last start tried to turn on", null=True
            ),
        ),
        migrations.AddField(
            model_name="competitionconfig",
            name="start_broadcasts",
            field=models.JSONField(
                blank=True, default=list, help_text="The DiscordTasks a start queued to post the start message"
            ),
        ),
        migrations.AddField(
            model_name="competitionconfig",
            name="started_this_window",
            field=models.BooleanField(
                default=False,
                help_text="A start ran since the start time was last set, the end was last cleared or a stop came with no "
                "end",
            ),
        ),
        migrations.RunPython(record_last_runs, migrations.RunPython.noop),
    ]
