"""A finding's access level is one choice, and each choice carries its points to the submit page."""

import re
from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse

from scoring.models import AttackType, RedTeamScore

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(("level", "points"), [("none", 0), ("user", 25), ("root", 100)])
def test_the_access_level_scores_its_points(level: str, points: int) -> None:
    assert RedTeamScore(access_level=level).calculate_points() == Decimal(points)


def test_privilege_escalation_adds_to_the_access_level() -> None:
    finding = RedTeamScore(access_level="user", privilege_escalation=True, credentials_recovered=True)
    assert finding.calculate_points() == Decimal(25 + 100 + 50)
    assert finding.outcomes_display == ["User Access (-25)", "Privilege Escalation (-100)", "Credentials (-50)"]


def test_each_access_level_choice_carries_its_points(red_team_user: User) -> None:
    client = Client()
    client.force_login(red_team_user)
    html = client.get(reverse("scoring:submit_red_score")).content.decode()
    radios = re.findall(r'<input[^>]*name="access_level"[^>]*>', html)
    points = {re.search(r'value="(\w+)"', r).group(1): re.search(r'data-points="(\d+)"', r).group(1) for r in radios}
    assert points == {"none": "0", "user": "25", "root": "100"}


def test_a_finding_posted_without_an_access_level_is_refused(red_team_user: User, blue_team) -> None:
    """A page from before the access level was one choice posts root_access instead: an error, not 0 points."""
    attack = AttackType.objects.create(name="Credential reuse")
    client = Client()
    client.force_login(red_team_user)
    response = client.post(
        reverse("scoring:submit_red_score"),
        {
            "source_ip_type": "single",
            "source_ip": "10.0.0.9",
            "attack_type": attack.id,
            "root_access": "on",
            "affected_teams": [blue_team.id],
        },
    )
    assert response.status_code == 200
    assert "access_level" in response.context["form"].errors
    assert not RedTeamScore.objects.exists()
