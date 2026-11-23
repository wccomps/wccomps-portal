"""post_missing_ticket_panels queues a plain (not recreating) channel setup for each active team with channels that
isn't marked channels_need_clearing."""

from io import StringIO

import pytest
from django.core.management import call_command

from core.models import QueuedTask
from team.models import Team, default_team_name, team_group_name


@pytest.mark.django_db
def test_queues_a_setup_for_each_active_team_with_channels_not_being_cleared_and_recreates_none():
    for n in (1, 2, 5, 7, 9):
        Team.objects.create(team_number=n, team_name=default_team_name(n), authentik_group=team_group_name(n))
    Team.objects.filter(team_number__in=[2, 5]).update(discord_role_id=11, discord_category_id=12)
    Team.objects.filter(team_number=7).update(discord_role_id=13, discord_category_id=14, is_active=False)
    Team.objects.filter(team_number=9).update(discord_role_id=15, discord_category_id=16, channels_need_clearing=True)
    out = StringIO()

    call_command("post_missing_ticket_panels", stdout=out)

    queued = QueuedTask.objects.filter(task_type="setup_team_infrastructure").order_by("pk")
    assert [(t.backend, t.payload) for t in queued] == [
        ("discord", {"team_number": 2, "recreate": False}),
        ("discord", {"team_number": 5, "recreate": False}),
    ]
    assert "2 team(s): 2, 5" in out.getvalue()
