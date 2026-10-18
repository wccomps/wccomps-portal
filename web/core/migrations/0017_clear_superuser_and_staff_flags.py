from django.conf import settings
from django.db import migrations


def clear_superuser_and_staff_flags(apps, schema_editor):
    """PortalBackend and AuthentikAdminSite grant permissions from UserGroups, not Django's auth_user flags;
    User.has_perm still short-circuits on is_superuser before calling any backend, so clear legacy flags."""
    User = apps.get_model("auth", "User")
    User.objects.filter(is_superuser=True).update(is_superuser=False)
    User.objects.filter(is_staff=True).update(is_staff=False)


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0016_competitionconfig_start_message"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RunPython(clear_superuser_and_staff_flags, migrations.RunPython.noop),
    ]
