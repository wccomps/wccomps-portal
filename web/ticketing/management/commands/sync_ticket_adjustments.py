from django.core.management.base import BaseCommand

from ticketing.scoring_sync import recompute_all_ticket_adjustments


class Command(BaseCommand):
    help = "Recompute every team's ticket-charge point_adjustments from approved tickets."

    def handle(self, *args: str, **options: object) -> None:
        count = recompute_all_ticket_adjustments()
        self.stdout.write(self.style.SUCCESS(f"Recomputed ticket adjustments for {count} teams."))
