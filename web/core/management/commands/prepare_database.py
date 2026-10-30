"""Apply migrations and seed teams, one container at a time.

Every web container runs this at start (web/entrypoint.sh). Django's migrate takes no lock, so two
containers starting together would both apply the same migration; a Postgres advisory lock makes
them take turns, and the later ones find nothing left to do.
"""

from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import connection

from core.utils import advisory_lock
from team.models import Team

# Arbitrary, but fixed: every container must ask for the same lock.
LOCK_ID = 7_261_640_001


class Command(BaseCommand):
    help = "Apply migrations and seed teams if there are none, holding a database-wide lock"

    def handle(self, *args: str, **options: int) -> None:
        verbosity = options.get("verbosity", 1)
        with connection.cursor() as cursor:
            # The settings' 30s statement_timeout would cancel the wait for the lock while another
            # container migrates, and any slow migration; neither should be cut off.
            cursor.execute("SET statement_timeout = 0")
        with advisory_lock(LOCK_ID, wait=True):
            call_command("migrate", interactive=False, verbosity=verbosity)
            if not Team.objects.exists():
                call_command("init_teams", verbosity=verbosity)
