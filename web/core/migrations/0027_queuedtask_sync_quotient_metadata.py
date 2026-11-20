from django.db import migrations, models


class Migration(migrations.Migration):
    """The worker's Quotient metadata sync, which a competition start queues, joins QueuedTask.task_type's choices."""

    dependencies = [
        ("core", "0026_queuedtask_worker_tasks"),
    ]

    operations = [
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
                    ("reconcile_nextcloud", "Reconcile Nextcloud Accounts"),
                    ("reset_nextcloud_team", "Reset a Team's Nextcloud Account"),
                    ("sync_quotient_metadata", "Sync Quotient Metadata"),
                ],
                max_length=50,
            ),
        ),
    ]
