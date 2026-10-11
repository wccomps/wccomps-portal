import django.db.models.deletion
from django.db import migrations, models


def link_schools(apps, schema_editor):
    """Give every existing assignment (and its registration) the school of its registration's name."""
    School = apps.get_model("registration", "School")
    EventTeamAssignment = apps.get_model("registration", "EventTeamAssignment")
    claimed = {}
    for assignment in EventTeamAssignment.objects.select_related("registration").order_by("pk"):
        name = " ".join(assignment.registration.school_name.split())
        key = (assignment.event_id, name.casefold())
        if key in claimed:
            raise RuntimeError(
                f"Event {assignment.event_id} has two assignments for the school {name!r} "
                f"(registrations {claimed[key]} and {assignment.registration_id}). "
                "Rename one registration first, then migrate again."
            )
        claimed[key] = assignment.registration_id
        school, _ = School.objects.get_or_create(name_key=name.casefold(), defaults={"name": name})
        assignment.school = school
        assignment.save(update_fields=["school"])
        assignment.registration.school = school
        assignment.registration.save(update_fields=["school"])


class Migration(migrations.Migration):
    dependencies = [("registration", "0004_registration_rate_limit")]

    operations = [
        migrations.CreateModel(
            name="School",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=255)),
                ("name_key", models.CharField(max_length=255, unique=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={"ordering": ["name"]},
        ),
        migrations.AddField(
            model_name="teamregistration",
            name="school",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="registrations",
                to="registration.school",
            ),
        ),
        migrations.AddField(
            model_name="eventteamassignment",
            name="school",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="assignments",
                to="registration.school",
            ),
        ),
        migrations.RunPython(link_schools, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="eventteamassignment",
            name="school",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT, related_name="assignments", to="registration.school"
            ),
        ),
        migrations.RemoveConstraint(model_name="eventteamassignment", name="unique_assignment_per_registration"),
        migrations.AddConstraint(
            model_name="eventteamassignment",
            constraint=models.UniqueConstraint(fields=("event", "school"), name="unique_assignment_per_school"),
        ),
        migrations.AlterField(
            model_name="eventteamassignment",
            name="registration",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="team_assignments",
                to="registration.teamregistration",
            ),
        ),
    ]
