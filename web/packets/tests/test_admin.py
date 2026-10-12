"""The packet admin can't set a packet's event or status: sending a packet records them."""

from datetime import date

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from registration.models import Event, Season

from core.models import UserGroups
from packets.models import Packet

pytestmark = pytest.mark.django_db


@pytest.fixture
def staff_client():
    admin = User.objects.create(username="admin", is_staff=True, is_superuser=True)
    UserGroups.objects.update_or_create(user=admin, defaults={"groups": ["WCComps_Discord_Admin"], "authentik_id": "x"})
    client = Client()
    client.force_login(admin)
    return client


def _packet(title="Packet", status="draft", event=None):
    return Packet.objects.create(
        title=title,
        file_data=b"x",
        filename="p.pdf",
        mime_type="application/pdf",
        file_size=1,
        uploaded_by="gold",
        status=status,
        event=event,
    )


def test_the_change_page_has_no_event_or_status_input(staff_client):
    season = Season.objects.create(name="2026", year=2026, is_active=True)
    event = Event.objects.create(
        season=season, name="Inv", event_type="invitational", date=date(2026, 10, 3), is_active=True
    )
    packet = _packet()
    url = reverse("admin:packets_packet_change", args=[packet.pk])

    page = staff_client.get(url).content.decode()
    response = staff_client.post(
        url,
        {
            "title": "Renamed",
            "event": event.pk,
            "status": "completed",
            "filename": "p.pdf",
            "notes": "",
            "uploaded_by": "gold",
            "send_via_email": "on",
            "web_access_enabled": "on",
            "distributions-TOTAL_FORMS": "0",
            "distributions-INITIAL_FORMS": "0",
        },
    )

    assert 'name="event"' not in page
    assert 'name="status"' not in page
    assert response.status_code == 302
    packet.refresh_from_db()
    assert packet.title == "Renamed"
    assert packet.status == "draft"
    assert packet.event is None


def test_marking_completed_completes_only_packets_being_sent(staff_client):
    draft = _packet("Draft")
    sending = _packet("Sending", status="distributing")

    response = staff_client.post(
        reverse("admin:packets_packet_changelist"),
        {"action": "mark_as_completed", "_selected_action": [draft.pk, sending.pk]},
        follow=True,
    )

    draft.refresh_from_db()
    sending.refresh_from_db()
    assert draft.status == "draft"
    assert sending.status == "completed"
    content = response.content.decode()
    assert "Marked 1 packet(s) as completed." in content
    assert "Skipped Draft: only a packet being sent can be marked completed." in content


def test_a_sent_packet_cannot_be_deleted(staff_client):
    sent = _packet("Sent", status="completed")
    draft = _packet("Draft")

    sent_page = staff_client.get(reverse("admin:packets_packet_delete", args=[sent.pk]))
    draft_page = staff_client.get(reverse("admin:packets_packet_delete", args=[draft.pk]))
    staff_client.post(
        reverse("admin:packets_packet_changelist"),
        {"action": "delete_selected", "_selected_action": [sent.pk, draft.pk], "post": "yes"},
    )

    assert sent_page.status_code == 403
    assert draft_page.status_code == 200
    assert Packet.objects.filter(pk=sent.pk).exists()
