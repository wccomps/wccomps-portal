"""In a plain django.test.TestCase, a run_detached view's operation runs inline in the TestCase's transaction."""

import json
import threading
from datetime import date

from django.test import TestCase
from django.urls import reverse
from registration.models import Event, Season

from core.tests.roles import role_user
from packets.models import Packet


class DetachedViewInATestCase(TestCase):
    def test_a_streamed_resend_sees_the_test_cases_rows(self) -> None:
        season = Season.objects.create(name="2026", year=2026, is_active=True)
        event = Event.objects.create(
            season=season, name="Invitational", event_type="invitational", date=date(2026, 10, 3), is_active=True
        )
        packet = Packet.with_file(b"%PDF", "rules.pdf", event=event, title="Rules", uploaded_by="gold")
        packet.save()
        self.client.force_login(role_user("gold_team"))

        response = self.client.post(reverse("packet_action", args=[packet.pk]), {"action": "resend_failed"})
        done = json.loads(b"".join(response.streaming_content).decode().splitlines()[-1])

        self.assertEqual(done["message"], "No failed distributions to resend")
        self.assertFalse([t for t in threading.enumerate() if t.name == "run-detached"])
