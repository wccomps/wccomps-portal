"""Team-supplied ticket text renders escaped on the ticket page, including through cotton component props."""

import pytest
from django.test import Client
from django.urls import reverse

from team.models import Team
from ticketing.models import Ticket, TicketCategory, TicketComment

pytestmark = pytest.mark.django_db

PAYLOAD = '<script>alert("x")</script>'


@pytest.mark.parametrize("user_fixture", ["blue_team_user", "ticketing_support_user"])
def test_title_description_and_comment_are_escaped(request, user_fixture):
    user = request.getfixturevalue(user_fixture)
    team = Team.objects.update_or_create(team_number=1, defaults={"team_name": "Blue Team 01"})[0]
    ticket = Ticket.objects.create(
        ticket_number="T001-001",
        team=team,
        category=TicketCategory.objects.get(pk=6),
        title=f"title {PAYLOAD}",
        description=f"description {PAYLOAD}",
    )
    TicketComment.objects.create(ticket=ticket, author=user, comment_text=f"comment {PAYLOAD}")
    client = Client()
    client.force_login(user)

    content = client.get(reverse("ticket_detail", args=[ticket.ticket_number])).content.decode()

    assert PAYLOAD not in content
    for field in ("title", "description", "comment"):
        assert f"{field} &lt;script&gt;" in content
