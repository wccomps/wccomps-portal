from django.db import migrations, models


class Migration(migrations.Migration):
    """DiscordTask becomes QueuedTask with a backend per row; rows already queued are Discord's (the default)."""

    dependencies = [
        ("core", "0024_scrub_start_broadcasts"),
    ]

    operations = [
        # The table keeps its name, so pods on the previous release use the same table while a deploy rolls
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.RenameModel(old_name="DiscordTask", new_name="QueuedTask"),
                migrations.AlterModelTable(name="queuedtask", table="core_discordtask"),
            ],
        ),
        migrations.AddField(
            model_name="queuedtask",
            name="backend",
            field=models.CharField(
                choices=[("discord", "Discord"), ("zulip", "Zulip"), ("worker", "Worker")],
                default="discord",
                db_default="discord",
                max_length=20,
            ),
        ),
        migrations.RemoveIndex(model_name="queuedtask", name="core_discor_status_6ec6f0_idx"),
        migrations.AddIndex(
            model_name="queuedtask",
            index=models.Index(fields=["backend", "status", "next_retry_at"], name="core_discor_backend_6c30d8_idx"),
        ),
        migrations.AlterField(
            model_name="competitionconfig",
            name="start_broadcasts",
            field=models.JSONField(
                blank=True,
                default=list,
                help_text="The QueuedTasks (one per chat backend) a start queued to post the start message",
            ),
        ),
        migrations.AlterField(
            model_name="queuedtask",
            name="task_type",
            field=models.CharField(
                choices=[
                    ("post_comment", "Post Comment to Thread"),
                    ("broadcast_message", "Broadcast Message"),
                    ("log_to_channel", "Log to Ops Channel"),
                    ("post_ticket_update", "Post Ticket Update to Thread"),
                    ("ticket_created_web", "Ticket Created"),
                    ("competition_status_changed", "Competition Status Changed"),
                    ("sync_member_roles", "Sync a Member's Roles"),
                    ("setup_team_infrastructure", "Setup Team Infrastructure"),
                    ("sync_roles", "Sync Roles from Authentik Groups"),
                    ("add_user_to_thread", "Add User to Thread"),
                    ("reconcile_teams", "Reconcile Team Channels"),
                    ("clear_team_channels", "Clear a Team's Old Channels"),
                ],
                max_length=50,
            ),
        ),
    ]
