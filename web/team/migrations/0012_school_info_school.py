import django.db.models.deletion
from django.db import migrations, models


def link_schools(apps, schema_editor):
    """Point every SchoolInfo at the School of its name, matched ignoring case and runs of whitespace."""
    School = apps.get_model("registration", "School")
    SchoolInfo = apps.get_model("team", "SchoolInfo")
    for school_info in SchoolInfo.objects.order_by("pk"):
        name = " ".join(school_info.school_name.split())
        school, _ = School.objects.get_or_create(name_key=name.casefold(), defaults={"name": name})
        school_info.school = school
        school_info.save(update_fields=["school"])


def copy_names_back(apps, schema_editor):
    SchoolInfo = apps.get_model("team", "SchoolInfo")
    for school_info in SchoolInfo.objects.select_related("school"):
        school_info.school_name = school_info.school.name
        school_info.save(update_fields=["school_name"])


class Migration(migrations.Migration):
    dependencies = [
        ("team", "0011_school_info_password_help_reset_password"),
        ("registration", "0006_one_open_event"),
    ]

    operations = [
        migrations.AddField(
            model_name="schoolinfo",
            name="school",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="school_infos",
                to="registration.school",
            ),
        ),
        migrations.RunPython(link_schools, copy_names_back),
        migrations.AlterField(
            model_name="schoolinfo",
            name="school",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT, related_name="school_infos", to="registration.school"
            ),
        ),
        # The default lets a reverse re-add the column on a table with rows, before copy_names_back fills it
        migrations.AlterField(
            model_name="schoolinfo", name="school_name", field=models.CharField(default="", max_length=255)
        ),
        migrations.RemoveField(model_name="schoolinfo", name="school_name"),
    ]
