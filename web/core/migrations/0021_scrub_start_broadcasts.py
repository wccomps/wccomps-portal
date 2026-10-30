from django.db import migrations

SCRUBBED = "(start message removed when the event ended)"


def scrub_start_broadcasts(apps, schema_editor):
    """Start broadcasts from before starts recorded them (the stored message, or the start's sender and target):
    finished ones are scrubbed, unfinished ones (possibly a start running now) left to the next reset."""
    CompetitionConfig = apps.get_model("core", "CompetitionConfig")
    DiscordTask = apps.get_model("core", "DiscordTask")
    config = CompetitionConfig.objects.filter(pk=1).first()
    message = config.start_message.strip() if config else ""
    unfinished = []
    for task in DiscordTask.objects.filter(task_type="broadcast_message"):
        payload = task.payload or {}
        sent_by_start = payload.get("sender") == "Black Team" and payload.get("target") == "all-teams"
        if not (sent_by_start or (message and payload.get("message") == config.start_message)):
            continue
        if task.status in ("pending", "processing"):
            unfinished.append(task.pk)
        elif payload.get("message") != SCRUBBED:
            task.payload = {**payload, "message": SCRUBBED}
            task.save(update_fields=["payload"])
    if config is not None and unfinished:
        config.start_broadcasts = sorted({*config.start_broadcasts, *unfinished})
        config.save(update_fields=["start_broadcasts"])


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0020_discordtask_cleanup_jobs"),
    ]

    operations = [
        migrations.RunPython(scrub_start_broadcasts, migrations.RunPython.noop),
    ]
