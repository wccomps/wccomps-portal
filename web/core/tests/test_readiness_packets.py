"""The packets check looks at this event's packets only: last event's packets went to last event's teams."""

from datetime import date

import pytest
from packets.models import Packet
from registration.models import Event, Season

from core.admin_views.readiness import _check_packets_distributed
from team.models import Team

pytestmark = pytest.mark.django_db


def _packet(title, event):
    return Packet.objects.create(
        event=event,
        title=title,
        file_data=b"%PDF",
        filename="rules.pdf",
        mime_type="application/pdf",
        file_size=4,
        status="completed",
        uploaded_by="gold",
    )


@pytest.fixture
def season():
    Team.objects.create(team_number=1, team_name="Team 01", is_active=True)
    return Season.objects.create(name="2026", year=2026, is_active=True)


def _event(season, name, **fields):
    return Event.objects.create(season=season, name=name, event_type="invitational", date=date(2026, 10, 3), **fields)


def test_only_the_open_events_packets_are_checked(season):
    _packet("Old Rules", _event(season, "Inv"))
    _packet("New Rules", _event(season, "Qual", is_active=True))
    _packet("Unsent Rules", None)

    status, message, _ = _check_packets_distributed()

    assert status == "warn"
    assert message == "New Rules: 1 teams missing"


def test_with_no_open_event_only_packets_without_an_event_are_checked(season):
    _packet("Old Rules", _event(season, "Inv"))
    _packet("Rules", None)

    status, message, _ = _check_packets_distributed()

    assert status == "warn"
    assert message == "Rules: 1 teams missing"


def test_last_events_packets_alone_pass(season):
    _packet("Old Rules", _event(season, "Inv"))

    assert _check_packets_distributed()[0] == "pass"
