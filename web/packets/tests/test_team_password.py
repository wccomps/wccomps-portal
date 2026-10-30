"""A packet email carries the team's password: concurrent sends set it once and share it."""

import threading
import time
from unittest.mock import patch

import pytest
from django.db import close_old_connections
from registration.models import School

from packets.services import PacketDistributionService
from team.models import SchoolInfo, Team

# The two sends run in their own threads, which see only committed rows
pytestmark = pytest.mark.django_db(transaction=True)


def test_two_concurrent_sends_reset_the_password_once():
    team = Team.objects.create(team_number=1, team_name="Team 01", is_active=True)
    SchoolInfo.objects.create(team=team, school=School.for_name("School One"), contact_email="c@example.edu")
    resets = []

    def reset(_user, password):
        resets.append(password)
        time.sleep(0.3)

    passwords = []

    def send():
        try:
            passwords.append(PacketDistributionService()._team_password(team))
        finally:
            close_old_connections()

    with patch("core.authentik_utils.AuthentikManager") as cls:
        manager = cls.return_value.__enter__.return_value
        manager.find_user.return_value = {"pk": 5, "username": "team01", "uid": "sub-01"}
        manager.set_password.side_effect = reset
        threads = [threading.Thread(target=send) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    assert len(resets) == 1
    assert passwords == resets * 2
    assert SchoolInfo.objects.get(team=team).password == resets[0]
