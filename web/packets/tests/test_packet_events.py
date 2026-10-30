"""A team sees only the open event's packets; sending stamps the packet with the open event."""

import json
from datetime import date
from unittest.mock import MagicMock, patch

import pytest
from django.test import Client, TestCase
from django.urls import reverse
from registration.models import Event

from core.tests.detached import join_detached_runs
from packets.models import Packet, PacketDistribution
from packets.services import NO_EVENT_OPEN, NO_OPEN_EVENT, PacketDistributionService

pytestmark = pytest.mark.django_db


def _packet(event, title="Rules", status="completed"):
    return Packet.objects.create(
        event=event,
        title=title,
        file_data=b"%PDF",
        filename="rules.pdf",
        mime_type="application/pdf",
        file_size=4,
        status=status,
        uploaded_by="gold",
    )


@pytest.fixture
def team_1(db):
    from team.models import Team

    return Team.objects.get_or_create(team_number=1, defaults={"team_name": "Team 01", "is_active": True})[0]


def test_team_sees_only_the_open_events_packets(blue_team_user, team_1, current_event):
    earlier = Event.objects.create(
        season=current_event.season, name="Earlier", event_type="invitational", date=date(2026, 9, 1)
    )
    PacketDistribution.objects.create(packet=_packet(earlier, "Old"), team=team_1)
    PacketDistribution.objects.create(packet=_packet(current_event, "Current"), team=team_1)
    client = Client()
    client.force_login(blue_team_user)

    content = client.get(reverse("team_packet")).content.decode()

    assert "Current" in content
    assert "Old" not in content


def test_an_earlier_events_packet_cannot_be_downloaded(blue_team_user, team_1, current_event):
    earlier = Event.objects.create(
        season=current_event.season, name="Earlier", event_type="invitational", date=date(2026, 9, 1)
    )
    packet = _packet(earlier)
    PacketDistribution.objects.create(packet=packet, team=team_1)
    client = Client()
    client.force_login(blue_team_user)

    assert client.get(reverse("download_packet", args=[packet.id])).status_code == 404


def test_no_open_event_means_no_packets(blue_team_user, team_1):
    packet = _packet(None)
    PacketDistribution.objects.create(packet=packet, team=team_1)
    client = Client()
    client.force_login(blue_team_user)

    assert client.get(reverse("download_packet", args=[packet.id])).status_code == 404


def test_sending_stamps_the_open_event(current_event):
    packet = _packet(None, status="draft")

    lines = list(PacketDistributionService().stream_distribute_packet(packet))

    packet.refresh_from_db()
    assert packet.event == current_event
    assert json.loads(lines[-1])["done"]


def test_sending_with_no_open_event_is_refused():
    packet = _packet(None, status="draft")

    lines = list(PacketDistributionService().stream_distribute_packet(packet))

    result = json.loads(lines[-1])
    assert result == {"done": True, "success": False, "message": NO_OPEN_EVENT}
    packet.refresh_from_db()
    assert packet.status == "draft"


def test_distribute_packet_stamps_the_open_event(current_event):
    packet = _packet(None, status="draft")
    packet.send_via_email = False
    packet.save()

    PacketDistributionService().distribute_packet(packet)

    packet.refresh_from_db()
    assert packet.event == current_event


def test_distribute_packet_with_no_open_event_is_refused():
    packet = _packet(None, status="draft")

    with pytest.raises(ValueError, match="No event is open. Import the next school list"):
        PacketDistributionService().distribute_packet(packet)

    packet.refresh_from_db()
    assert packet.status == "draft"


def test_admin_distribute_now_shows_the_refusal():
    from django.contrib import admin, messages

    from packets.admin import PacketAdmin

    packet = _packet(None, status="draft")
    model_admin = PacketAdmin(Packet, admin.site)

    with patch.object(model_admin, "message_user") as message_user:
        model_admin.distribute_now(MagicMock(), Packet.objects.filter(pk=packet.pk))

    texts = [call.args[1] for call in message_user.call_args_list]
    assert any(NO_OPEN_EVENT in t for t in texts)
    assert message_user.call_args_list[0].kwargs["level"] == messages.ERROR
    packet.refresh_from_db()
    assert packet.status == "draft"


def test_resending_an_earlier_events_packet_is_refused(current_event, team_1):
    earlier = Event.objects.create(
        season=current_event.season, name="Earlier", event_type="invitational", date=date(2026, 9, 1)
    )
    packet = _packet(earlier)
    PacketDistribution.objects.create(packet=packet, team=team_1, email_status="failed")

    result = json.loads(list(PacketDistributionService().stream_resend_failed(packet))[-1])

    assert result["success"] is False
    assert "earlier event" in result["message"]
    assert PacketDistribution.objects.get(packet=packet).email_status == "failed"


def test_resending_to_one_team_for_an_earlier_event_is_refused(gold_team_user, current_event, team_1):
    earlier = Event.objects.create(
        season=current_event.season, name="Earlier", event_type="invitational", date=date(2026, 9, 1)
    )
    packet = _packet(earlier)
    PacketDistribution.objects.create(packet=packet, team=team_1, email_status="failed")
    client = Client()
    client.force_login(gold_team_user)

    response = client.post(
        reverse("packet_resend_team", args=[packet.id, team_1.id]), {"primary_email": "a@example.edu"}, follow=True
    )

    assert "earlier event" in response.content.decode()
    assert PacketDistribution.objects.get(packet=packet).email_status == "failed"


def test_a_completed_packet_from_an_earlier_event_cannot_be_distributed(current_event):
    earlier = Event.objects.create(
        season=current_event.season, name="Earlier", event_type="invitational", date=date(2026, 9, 1)
    )
    packet = _packet(earlier, status="completed")

    lines = list(PacketDistributionService().stream_distribute_packet(packet))

    result = json.loads(lines[-1])
    assert result["success"] is False
    assert "draft" in result["message"]
    packet.refresh_from_db()
    assert packet.event == earlier
    assert packet.status == "completed"


def test_admin_retry_for_an_earlier_events_packet_is_refused(current_event, team_1):
    from django.contrib import admin, messages

    from packets.admin import PacketDistributionAdmin

    earlier = Event.objects.create(
        season=current_event.season, name="Earlier", event_type="invitational", date=date(2026, 9, 1)
    )
    dist = PacketDistribution.objects.create(packet=_packet(earlier), team=team_1, email_status="failed")
    model_admin = PacketDistributionAdmin(PacketDistribution, admin.site)

    with (
        patch.object(model_admin, "message_user") as message_user,
        patch.object(PacketDistributionService, "send_packet_email") as send,
    ):
        model_admin.retry_failed_emails(MagicMock(), PacketDistribution.objects.filter(pk=dist.pk))

    send.assert_not_called()
    assert message_user.call_args_list[0].kwargs["level"] == messages.ERROR
    assert "earlier event" in message_user.call_args_list[0].args[1]


def test_resending_to_a_team_with_no_school_on_file_is_refused(gold_team_user, current_event, team_1):
    from team.models import SchoolInfo

    packet = _packet(current_event)
    PacketDistribution.objects.create(packet=packet, team=team_1, email_status="failed")
    client = Client()
    client.force_login(gold_team_user)

    with patch.object(PacketDistributionService, "send_packet_email") as send:
        response = client.post(
            reverse("packet_resend_team", args=[packet.id, team_1.id]), {"primary_email": "a@example.edu"}, follow=True
        )

    assert "Team 1 has no school on file. Add it on the School Info page first." in response.content.decode()
    send.assert_not_called()
    assert not SchoolInfo.objects.filter(team=team_1).exists()
    assert PacketDistribution.objects.get(packet=packet).email_status == "failed"


def test_resending_with_no_open_event_says_no_event_is_open(current_event):
    from packets.services import check_resendable

    packet = _packet(current_event)
    current_event.is_active = False
    current_event.save()

    assert check_resendable(packet) == NO_EVENT_OPEN


def test_resending_a_packet_sent_before_events_were_recorded_is_refused(current_event):
    from packets.services import check_resendable

    refusal = check_resendable(_packet(None))

    assert refusal is not None
    assert "earlier event" not in refusal
    assert "before events were recorded" in refusal


def _admin_retry(*dists):
    from django.contrib import admin

    from packets.admin import PacketDistributionAdmin

    model_admin = PacketDistributionAdmin(PacketDistribution, admin.site)
    with patch.object(model_admin, "message_user") as message_user, TestCase.captureOnCommitCallbacks(execute=True):
        model_admin.retry_failed_emails(MagicMock(), PacketDistribution.objects.filter(pk__in=[d.pk for d in dists]))
    return [call.args[1] for call in message_user.call_args_list]


@pytest.mark.django_db(transaction=True)  # the resend runs in threads of its own, as in production
def test_the_admin_retry_records_a_repeat_failure(current_event, team_1):
    dist = PacketDistribution.objects.create(
        packet=_packet(current_event), team=team_1, email_status="failed", email_error_message="Earlier failure"
    )

    with patch.object(PacketDistributionService, "send_packet_email", side_effect=RuntimeError("Mail server down")):
        _admin_retry(dist)
        join_detached_runs()

    dist.refresh_from_db()
    assert (dist.email_status, dist.email_error_message) == ("failed", "Mail server down")


def test_the_admin_retry_resends_only_the_selected_failed_emails(current_event, team_1):
    from team.models import Team

    packet = _packet(current_event)
    chosen = PacketDistribution.objects.create(packet=packet, team=team_1, email_status="failed")
    other_team = Team.objects.create(team_number=2, team_name="Team 02")
    PacketDistribution.objects.create(packet=packet, team=other_team, email_status="failed")

    with patch.object(PacketDistributionService, "send_packet_email") as send:
        said = _admin_retry(chosen)

    assert [(call.args[0].pk, call.args[0].email_status) for call in send.call_args_list] == [(chosen.pk, "pending")]
    assert "Rules" in said[0]


def test_the_admin_retry_sends_in_the_background(current_event, team_1):
    from packets import admin as packets_admin

    dist = PacketDistribution.objects.create(packet=_packet(current_event), team=team_1, email_status="failed")

    with (
        patch.object(packets_admin, "start_detached") as start,
        patch.object(PacketDistributionService, "send_packet_email") as send,
    ):
        _admin_retry(dist)

    send.assert_not_called()
    assert start.call_count == 1


@pytest.mark.django_db(transaction=True)  # the sends run in threads of their own, as in production
def test_two_overlapping_resends_send_each_email_once(current_event, team_1):
    from django.db.models import QuerySet

    from team.models import Team

    packet = _packet(current_event)
    team_2 = Team.objects.create(team_number=2, team_name="Team 02")
    for team in (team_1, team_2):
        PacketDistribution.objects.create(packet=packet, team=team, email_status="failed")
    sent: list[int] = []
    original_update = QuerySet.update
    overlapped = []

    def update(self, **kwargs):
        if self.model is PacketDistribution and not overlapped:
            overlapped.append(True)
            list(PacketDistributionService().stream_resend_failed(packet))
        return original_update(self, **kwargs)

    with (
        patch.object(QuerySet, "update", update),
        patch.object(PacketDistributionService, "send_packet_email", side_effect=lambda dist: sent.append(dist.pk)),
    ):
        list(PacketDistributionService().stream_resend_failed(packet))

    assert sorted(sent) == sorted(packet.distributions.values_list("pk", flat=True))
