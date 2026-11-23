from django.core.management.base import BaseCommand

from core.models import QueuedTask
from core.queued_tasks import SetupTeamInfrastructure
from team.models import Team


class Command(BaseCommand):
    help = (
        "Queue a channel setup, recreating nothing, for each active team with a recorded category that isn't marked "
        "channels_need_clearing. Setup creates only what is missing and posts the ticket panel only to a chat channel "
        "that lacks the bot's among its first 50 messages, so run it before those channels fill up."
    )

    def handle(self, *args: str, **options: object) -> None:
        teams = Team.objects.filter(is_active=True, discord_category_id__isnull=False, channels_need_clearing=False)
        numbers = list(teams.order_by("team_number").values_list("team_number", flat=True))
        for number in numbers:
            QueuedTask.enqueue(SetupTeamInfrastructure(team_number=number))
        listed = ", ".join(map(str, numbers)) or "none"
        self.stdout.write(f"Queued a channel setup for {len(numbers)} team(s): {listed}")
