from django.db import migrations


class Migration(migrations.Migration):
    """Drop the student helper columns of team_discordlink, which 0014 and 0015 removed from the model state but
    kept for pods on the previous release during those rollouts. Student helpers are retired, so their history
    goes with them.

    The lock timeout makes the drop fail, and the pod retry, when a long query holds the table, instead of
    queueing every request behind the ALTER. Reversing adds the columns back with their defaults, and the
    is_student_helper index, as those migrations left them, so a rollback reaches the keeping migrations; it
    stops working past core 0015, team 0008 and scoring 0038, whose drops reverse as no-ops.
    """

    dependencies = [
        ("team", "0015_discordlink_forget_student_helper"),
    ]

    operations = [
        migrations.RunSQL(
            [
                "SET LOCAL lock_timeout = '5s'",
                "ALTER TABLE team_discordlink DROP COLUMN IF EXISTS helper_activated_at, "
                "DROP COLUMN IF EXISTS helper_deactivated_at, "
                "DROP COLUMN IF EXISTS helper_removal_reason, "
                "DROP COLUMN IF EXISTS is_student_helper, "
                "DROP COLUMN IF EXISTS helper_role_name, "
                "DROP COLUMN IF EXISTS helper_role_id",
            ],
            [
                "SET LOCAL lock_timeout = '5s'",
                "ALTER TABLE team_discordlink "
                "ADD COLUMN IF NOT EXISTS helper_activated_at timestamp with time zone NULL, "
                "ADD COLUMN IF NOT EXISTS helper_deactivated_at timestamp with time zone NULL, "
                "ADD COLUMN IF NOT EXISTS helper_removal_reason text NOT NULL DEFAULT '', "
                "ADD COLUMN IF NOT EXISTS is_student_helper boolean NOT NULL DEFAULT false, "
                "ADD COLUMN IF NOT EXISTS helper_role_name varchar(100) NOT NULL DEFAULT '', "
                "ADD COLUMN IF NOT EXISTS helper_role_id bigint NULL",
                "CREATE INDEX IF NOT EXISTS team_discordlink_is_student_helper_5496ec0c "
                "ON team_discordlink (is_student_helper)",
            ],
        ),
    ]
