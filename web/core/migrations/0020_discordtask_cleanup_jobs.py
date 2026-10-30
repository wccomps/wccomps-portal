from django.db import migrations, models

OLD_ROW = (
    "Queued before this release, when the task also reset the database: run /competition cleanup-competition again"
)


def move_queued_cleanups(apps, schema_editor):
    """Pending or processing cleanup_competition rows become reconcile_teams rows: Close Out's (with
    closed_out_event_id) for no open event; older ones, which also reset the database, fail with a note."""
    DiscordTask = apps.get_model("core", "DiscordTask")
    waiting = DiscordTask.objects.filter(task_type="cleanup_competition", status__in=("pending", "processing"))
    waiting.filter(payload__closed_out_event_id__isnull=True).update(status="failed", error_message=OLD_ROW)
    for task in waiting.exclude(status="failed"):
        task.task_type = "reconcile_teams"
        task.payload = {"requested_by": task.payload["requested_by"], "open_event_id": None}
        task.save(update_fields=["task_type", "payload"])


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0019_competitionconfig_last_run"),
    ]

    operations = [
        migrations.AlterField(
            model_name="discordtask",
            name="task_type",
            field=models.CharField(
                choices=[
                    ("post_comment", "Post Comment to Thread"),
                    ("broadcast_message", "Broadcast Message"),
                    ("sync_member_roles", "Sync a Member's Roles"),
                    ("setup_team_infrastructure", "Setup Team Infrastructure"),
                    ("log_to_channel", "Log to Ops Channel"),
                    ("post_ticket_update", "Post Ticket Update to Thread"),
                    ("ticket_created_web", "Ticket Created via Web"),
                    ("sync_roles", "Sync Roles from Authentik Groups"),
                    ("add_user_to_thread", "Add User to Thread"),
                    ("reconcile_teams", "Reconcile Team Channels"),
                    ("clear_team_channels", "Clear a Team's Old Channels"),
                ],
                max_length=50,
            ),
        ),
        migrations.RunPython(move_queued_cleanups, migrations.RunPython.noop),
    ]
