"""A broadcast target is validated at the form, so a bad one is refused instead of queued and failed later."""

import pytest
from django.urls import reverse

from core.broadcast import AllTeams, Announcements, Teams, parse_broadcast_target
from core.forms import BroadcastForm
from core.models import DiscordTask


@pytest.mark.parametrize(
    ("target", "parsed"),
    [
        (" Announcements ", Announcements()),
        ("ALL-TEAMS", AllTeams()),
        ("1,3-4", Teams((1, 3, 4))),
    ],
)
def test_targets_parse_to_their_kind(target: str, parsed: object) -> None:
    assert parse_broadcast_target(target) == parsed


@pytest.mark.parametrize(
    ("target", "reason"), [("9-3", "Invalid range"), ("teams", "Invalid team number"), (",", "no team")]
)
def test_a_bad_target_is_a_value_error(target: str, reason: str) -> None:
    with pytest.raises(ValueError, match=reason):
        parse_broadcast_target(target)


def test_the_form_refuses_a_bad_range() -> None:
    form = BroadcastForm({"target": "9-3", "message": "hello"})

    assert not form.is_valid()
    assert "Invalid range" in form.errors["target"][0]


@pytest.mark.django_db
def test_a_bad_range_is_not_queued(client) -> None:
    from core.tests.roles import role_user

    client.force_login(role_user("admin"))

    response = client.post(reverse("admin_broadcast_action"), {"target": "9-3", "message": "hello"})

    assert response.status_code == 400
    assert "Invalid range" in response.json()["error"]
    assert not DiscordTask.objects.exists()
