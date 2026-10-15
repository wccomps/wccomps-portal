from django.core.management.color import no_style
from django.db import migrations


def move_id_sequence_past_seeds(apps, schema_editor):
    """0009 seeded categories 1 to 6 with explicit ids, which leaves the id sequence at 1: the first new
    categories collided with them. Move it past the highest id."""
    connection = schema_editor.connection
    TicketCategory = apps.get_model("ticketing", "TicketCategory")
    with connection.cursor() as cursor:
        for sql in connection.ops.sequence_reset_sql(no_style(), [TicketCategory]):
            cursor.execute(sql)


class Migration(migrations.Migration):
    dependencies = [
        ("ticketing", "0015_ticketcategory_playbook"),
    ]

    operations = [
        migrations.RunPython(move_id_sequence_past_seeds, migrations.RunPython.noop),
    ]
