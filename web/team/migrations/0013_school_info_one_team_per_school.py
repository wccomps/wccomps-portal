from django.db import migrations, models


def refuse_shared_schools(apps, schema_editor):
    """Stop with the clashing teams named when two teams hold the same school, rather than fail on the constraint."""
    SchoolInfo = apps.get_model("team", "SchoolInfo")
    teams_by_school = {}
    for school_info in SchoolInfo.objects.select_related("school", "team").order_by("team__team_number"):
        teams_by_school.setdefault(school_info.school.name, []).append(school_info.team.team_number)
    shared = [
        f"{name} (" + ", ".join(f"Team {number}" for number in numbers) + ")"
        for name, numbers in teams_by_school.items()
        if len(numbers) > 1
    ]
    if shared:
        raise RuntimeError(
            "A school can hold only one team in the school list; these are shared: "
            + "; ".join(shared)
            + ". Change all but one team's school on the School Info page, then migrate again."
        )


class Migration(migrations.Migration):
    dependencies = [("team", "0012_school_info_school")]

    operations = [
        migrations.RunPython(refuse_shared_schools, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="schoolinfo",
            constraint=models.UniqueConstraint(fields=("school",), name="one_team_per_school"),
        ),
    ]
