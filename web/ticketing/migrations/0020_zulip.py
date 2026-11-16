from django.db import migrations, models


class Migration(migrations.Migration):
    """Zulip's columns: who a rate-limited comment came from on any backend (actor_key), the Zulip message a comment
    or attachment came from, and a ticket's Zulip topic.

    Pods still running the previous release write these tables during a rolling deploy, so each new column has a
    database default (or is nullable), and CommentRateLimit.discord_id leaves the model but keeps its column, made
    nullable so new rows need no value. A later release drops the column."""

    dependencies = [
        ("ticketing", "0019_ticket_discord_ticket_message_id"),
    ]

    operations = [
        migrations.AddField(
            model_name="commentratelimit",
            name="actor_key",
            field=models.CharField(db_default="", max_length=64),
        ),
        migrations.AddIndex(
            model_name="commentratelimit",
            index=models.Index(fields=["actor_key", "-posted_at"], name="ticketing_c_actor_k_6bfcac_idx"),
        ),
        migrations.RemoveIndex(
            model_name="commentratelimit",
            name="ticketing_c_discord_755fb8_idx",
        ),
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.RemoveField(model_name="commentratelimit", name="discord_id"),
            ],
            database_operations=[
                migrations.AlterField(
                    model_name="commentratelimit",
                    name="discord_id",
                    field=models.BigIntegerField(null=True),
                ),
            ],
        ),
        migrations.AddField(
            model_name="ticketcomment",
            name="zulip_message_id",
            field=models.BigIntegerField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="ticketattachment",
            name="zulip_message_id",
            field=models.BigIntegerField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="ticket",
            name="zulip_topic",
            field=models.CharField(blank=True, db_default="", default="", max_length=60),
        ),
    ]
