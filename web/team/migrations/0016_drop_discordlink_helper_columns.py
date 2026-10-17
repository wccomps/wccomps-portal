from django.db import migrations


class Migration(migrations.Migration):
    """Drop the student helper columns 0014 and 0015 removed from the model state but kept for pods on the previous
    release during those rollouts. Student helpers are retired, so their history goes with them. Reversing adds the columns back, empty and as
    those migrations left them, so the earlier migrations can be unapplied."""

    dependencies = [
        ("team", "0015_discordlink_forget_student_helper"),
    ]

    operations = [
        migrations.RunSQL(
            "ALTER TABLE team_discordlink DROP COLUMN IF EXISTS helper_activated_at, "
            "DROP COLUMN IF EXISTS helper_deactivated_at, "
            "DROP COLUMN IF EXISTS helper_removal_reason, "
            "DROP COLUMN IF EXISTS is_student_helper, "
            "DROP COLUMN IF EXISTS helper_role_name, "
            "DROP COLUMN IF EXISTS helper_role_id",
            "ALTER TABLE team_discordlink ADD COLUMN IF NOT EXISTS helper_activated_at timestamp with time zone NULL, "
            "ADD COLUMN IF NOT EXISTS helper_deactivated_at timestamp with time zone NULL, "
            "ADD COLUMN IF NOT EXISTS helper_removal_reason text NOT NULL DEFAULT '', "
            "ADD COLUMN IF NOT EXISTS is_student_helper boolean NOT NULL DEFAULT false, "
            "ADD COLUMN IF NOT EXISTS helper_role_name varchar(100) NOT NULL DEFAULT '', "
            "ADD COLUMN IF NOT EXISTS helper_role_id bigint NULL",
        ),
    ]
