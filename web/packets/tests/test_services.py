"""Tests for packet services."""

import uuid
from unittest.mock import MagicMock, patch

from django.test import TestCase
from registration.models import School

from core.authentik_manager import SESSIONS_HIDDEN
from core.models import AuditLog, DiscordTask
from team.models import SchoolInfo, Team

from ..models import Packet, PacketDistribution
from ..services import PacketDistributionService, _create_distributions_for_teams


class PacketDistributionServiceTestCase(TestCase):
    """Test PacketDistributionService."""

    def setUp(self):
        """Create test data."""
        self.service = PacketDistributionService()

        from datetime import date

        from registration.models import Event, Season

        season = Season.objects.create(name="2026", year=2026, is_active=True)
        Event.objects.create(
            season=season, name="Inv", event_type="invitational", date=date(2026, 10, 3), is_active=True
        )

        for i in range(1, 4):
            team = Team.objects.create(
                team_number=i,
                team_name=f"Team {i}",
                authentik_group=f"WCComps_BlueTeam{i:02d}",
                is_active=True,
            )
            SchoolInfo.objects.create(
                team=team,
                school=School.for_name(f"School {i}"),
                contact_email=f"team{i}@example.com",
                password=f"TestPass{i}!",
            )

        self.packet = Packet.objects.create(
            title="Test Packet",
            file_data=b"test file content",
            filename="test.pdf",
            mime_type="application/pdf",
            file_size=100,
            uploaded_by="testuser",
            status="draft",
            send_via_email=True,
            web_access_enabled=True,
        )

    def test_create_distributions_for_teams(self):
        """Test creating distributions for all teams."""
        created = _create_distributions_for_teams(self.packet)

        self.assertEqual(len(created), 3)
        self.assertEqual(PacketDistribution.objects.count(), 3)

        # Verify distributions created for all teams
        for i in range(1, 4):
            team = Team.objects.get(team_number=i)
            dist = PacketDistribution.objects.get(packet=self.packet, team=team)
            self.assertEqual(dist.email_status, "pending")

    @patch("packets.services.EmailMultiAlternatives")
    def test_send_packet_email(self, mock_email_class):
        """Test sending packet email."""
        team = Team.objects.get(team_number=1)
        distribution = PacketDistribution.objects.create(packet=self.packet, team=team)

        # Mock email sending
        mock_email = MagicMock()
        mock_email_class.return_value = mock_email

        self.service.send_packet_email(distribution)

        # Verify packet was attached
        mock_email.attach.assert_called_once_with("test.pdf", b"test file content", "application/pdf")

        # Verify email was sent
        mock_email.send.assert_called_once_with(fail_silently=False)

        # Verify distribution was marked as sent
        distribution.refresh_from_db()
        self.assertEqual(distribution.email_status, "sent")
        self.assertEqual(distribution.email_sent_to, "team1@example.com")
        self.assertIn("TestPass1!", mock_email_class.call_args.kwargs["body"])

    @patch("packets.services.EmailMultiAlternatives")
    def test_send_packet_email_includes_secondary_email(self, mock_email_class):
        """Both primary and secondary emails on SchoolInfo receive the packet email."""
        team = Team.objects.get(team_number=1)
        SchoolInfo.objects.filter(team=team).update(secondary_email="coach1@example.com")
        distribution = PacketDistribution.objects.create(packet=self.packet, team=team)
        mock_email_class.return_value = MagicMock()

        self.service.send_packet_email(distribution)

        self.assertEqual(mock_email_class.call_args.kwargs["to"], ["team1@example.com", "coach1@example.com"])
        distribution.refresh_from_db()
        self.assertEqual(distribution.email_sent_to, "team1@example.com, coach1@example.com")

    @patch("packets.services.EmailMultiAlternatives")
    def test_stream_distribute_packet_skips_emails_when_send_via_email_false(self, mock_email_class):
        """stream_distribute_packet creates web distributions and completes without sending email when disabled."""
        import json

        self.packet.send_via_email = False
        self.packet.save(update_fields=["send_via_email"])

        lines = list(self.service.stream_distribute_packet(self.packet))
        done = json.loads(lines[-1])
        self.assertTrue(done["success"])
        self.assertEqual(done["message"], "Distributed to 3 teams")
        mock_email_class.assert_not_called()
        self.packet.refresh_from_db()
        self.assertEqual(self.packet.status, "completed")
        self.assertEqual(PacketDistribution.objects.filter(packet=self.packet, email_status="pending").count(), 3)

    @patch("packets.services.EmailMultiAlternatives")
    def test_send_packet_email_with_team_extras(self, mock_email_class):
        """Test that per-team extras are passed to email context."""
        self.packet.team_extras = {
            "1": {"api_key": "sk-test-key", "max_spend_usd": "100"},
            "2": {"api_key": "sk-other-key", "max_spend_usd": "200"},
        }
        self.packet.save()

        team = Team.objects.get(team_number=1)
        distribution = PacketDistribution.objects.create(packet=self.packet, team=team)

        mock_email = MagicMock()
        mock_email_class.return_value = mock_email

        self.service.send_packet_email(distribution)

        mock_email.send.assert_called_once_with(fail_silently=False)
        distribution.refresh_from_db()
        self.assertEqual(distribution.email_status, "sent")

    @staticmethod
    def _ops_messages() -> list[str]:
        return [task.payload["message"] for task in DiscordTask.objects.filter(task_type="log_to_channel")]

    @patch("packets.services.EmailMultiAlternatives")
    def test_first_send_sets_the_password_and_later_sends_reuse_it(self, mock_email_class):
        """The packet carries the team's credentials: a resend or test email must not change them."""
        team = Team.objects.get(team_number=1)
        SchoolInfo.objects.filter(team=team).update(password="")
        distribution = PacketDistribution.objects.create(packet=self.packet, team=team)

        with patch("core.authentik_utils.AuthentikManager") as cls:
            manager = cls.return_value.__enter__.return_value
            manager.find_user.return_value = {"pk": 5, "username": "team01"}
            manager.user_sessions.return_value = []
            manager.end_sessions.return_value = None
            self.service.send_packet_email(distribution)
            self.service.send_test_packet_email(self.packet, team, "gold@example.com")

        manager.set_password.assert_called_once()
        manager.end_sessions.assert_called_once()
        self.assertEqual(
            self._ops_messages(),
            ["Credentials reset for team 1 by packet distribution: ended the team's Authentik sessions"],
        )
        password = manager.set_password.call_args.args[1]
        self.assertEqual(SchoolInfo.objects.get(team=team).password, password)
        for call in mock_email_class.call_args_list:
            self.assertIn(password, call.kwargs["body"])

    @patch("packets.services.EmailMultiAlternatives")
    def test_account_signed_in_before_its_packet_alerts_ops(self, mock_email_class):
        team = Team.objects.get(team_number=1)
        SchoolInfo.objects.filter(team=team).update(password="")
        distribution = PacketDistribution.objects.create(packet=self.packet, team=team)

        with patch("core.authentik_utils.AuthentikManager") as cls:
            manager = cls.return_value.__enter__.return_value
            manager.find_user.return_value = {"pk": 5, "username": "team01"}
            manager.user_sessions.side_effect = [[uuid.UUID(int=1), uuid.UUID(int=2)], []]
            manager.end_sessions.return_value = None
            self.service.send_packet_email(distribution)

        alert = self._ops_messages()[-1]
        self.assertIn("Team 1", alert)
        self.assertIn("2 Authentik sessions", alert)
        mock_email_class.return_value.send.assert_called_once()

    @patch("packets.services.EmailMultiAlternatives")
    def test_sessions_that_could_not_be_ended_alert_ops(self, mock_email_class):
        team = Team.objects.get(team_number=1)
        SchoolInfo.objects.filter(team=team).update(password="")
        distribution = PacketDistribution.objects.create(packet=self.packet, team=team)

        with patch("core.authentik_utils.AuthentikManager") as cls:
            manager = cls.return_value.__enter__.return_value
            manager.find_user.return_value = {"pk": 5, "username": "team01"}
            manager.user_sessions.side_effect = [[], [uuid.UUID(int=1)]]
            manager.end_sessions.return_value = "Authentik did not end sessions (HTTP 403)"
            self.service.send_packet_email(distribution)

        self.assertEqual(
            self._ops_messages(),
            [
                "Credentials reset for team 1 by packet distribution: could not end the team's Authentik sessions: "
                "Authentik did not end sessions (HTTP 403)"
            ],
        )

    @patch("packets.services.EmailMultiAlternatives")
    def test_an_uncounted_session_total_is_reported_once(self, mock_email_class):
        """The reset's own note already says why the sessions couldn't be checked."""
        team = Team.objects.get(team_number=1)
        SchoolInfo.objects.filter(team=team).update(password="")
        distribution = PacketDistribution.objects.create(packet=self.packet, team=team)

        with patch("core.authentik_utils.AuthentikManager") as cls:
            manager = cls.return_value.__enter__.return_value
            manager.find_user.return_value = {"pk": 5, "username": "team01"}
            manager.user_sessions.return_value = []
            manager.sees_any_sessions.return_value = False
            self.service.send_packet_email(distribution)

        (note,) = self._ops_messages()
        self.assertIn(SESSIONS_HIDDEN, note)

    @patch("packets.services.EmailMultiAlternatives")
    def test_failed_password_set_sends_nothing(self, mock_email_class):
        team = Team.objects.get(team_number=1)
        SchoolInfo.objects.filter(team=team).update(password="")
        distribution = PacketDistribution.objects.create(packet=self.packet, team=team)

        with (
            patch("core.authentik_utils.AuthentikManager") as cls,
            self.assertRaisesMessage(ValueError, "Failed to set Authentik password for team 1: User team01 not found"),
        ):
            cls.return_value.__enter__.return_value.find_user.return_value = None
            self.service.send_packet_email(distribution)

        mock_email_class.return_value.send.assert_not_called()
        self.assertTrue(AuditLog.objects.filter(action="team_password_reset", target_id=1).exists())
        self.assertEqual(
            self._ops_messages(), ["Credentials reset for team 1 by packet distribution failed: User team01 not found"]
        )
        self.assertEqual(SchoolInfo.objects.get(team=team).password, "")

    @patch("packets.services.EmailMultiAlternatives")
    def test_team_without_school_info_fails_clearly(self, mock_email_class):
        team = Team.objects.get(team_number=1)
        distribution = PacketDistribution.objects.create(packet=self.packet, team=team)
        SchoolInfo.objects.filter(team=team).delete()

        with self.assertRaisesMessage(ValueError, "No email address for team 1"):
            self.service.send_packet_email(distribution)
        with self.assertRaisesMessage(ValueError, "Team 1 has no school info; import the school list first"):
            self.service.send_test_packet_email(self.packet, team, "gold@example.com")

        mock_email_class.return_value.send.assert_not_called()

    @patch("packets.services.PacketDistributionService.send_packet_email")
    def test_distribute_packet(self, mock_send_email):
        """Test distributing packet."""
        result = self.service.distribute_packet(self.packet)

        # Verify distributions created
        self.assertEqual(result["created"], 3)
        self.assertEqual(PacketDistribution.objects.count(), 3)

        # Verify packet marked as distributing then completed
        self.packet.refresh_from_db()
        self.assertEqual(self.packet.status, "completed")
        self.assertIsNotNone(self.packet.actual_distribution_time)


class PacketCompletesItselfTestCase(TestCase):
    """The last successful send completes the packet, whoever is (or isn't) watching the stream."""

    def setUp(self):
        self.service = PacketDistributionService()
        self.packet = Packet.objects.create(
            title="Packet",
            file_data=b"x",
            filename="p.pdf",
            mime_type="application/pdf",
            file_size=1,
            uploaded_by="gold",
            status="distributing",
            send_via_email=True,
        )
        self.dists = []
        for i in (1, 2):
            team = Team.objects.create(team_number=i, team_name=f"Team {i}", is_active=True)
            SchoolInfo.objects.create(
                team=team, school=School.for_name(f"S{i}"), contact_email=f"t{i}@example.com", password="pw"
            )
            self.dists.append(PacketDistribution.objects.create(packet=self.packet, team=team))

    @patch("packets.services.EmailMultiAlternatives")
    def test_last_send_completes_the_packet(self, _email):
        self.service.send_packet_email(self.dists[0])
        self.packet.refresh_from_db()
        self.assertEqual(self.packet.status, "distributing")

        self.service.send_packet_email(self.dists[1])
        self.packet.refresh_from_db()
        self.assertEqual(self.packet.status, "completed")

    @patch("packets.services.EmailMultiAlternatives")
    def test_a_failed_team_keeps_it_distributing(self, _email):
        self.dists[0].mark_as_failed("bounced")

        self.service.send_packet_email(self.dists[1])

        self.packet.refresh_from_db()
        self.assertEqual(self.packet.status, "distributing")

    @patch("packets.services.EmailMultiAlternatives")
    def test_a_cancelled_packet_stays_cancelled(self, _email):
        Packet.objects.filter(pk=self.packet.pk).update(status="cancelled")
        self.dists[0].mark_as_sent("t1@example.com")

        self.service.send_packet_email(self.dists[1])

        self.packet.refresh_from_db()
        self.assertEqual(self.packet.status, "cancelled")

    def test_migration_completes_packets_whose_emails_all_went_out(self):
        from importlib import import_module

        from django.apps import apps

        migration = import_module("packets.migrations.0007_complete_finished_packets")
        for dist in self.dists:
            dist.mark_as_sent("x@example.com")
        unfinished = Packet.objects.create(
            title="Other",
            file_data=b"x",
            filename="o.pdf",
            mime_type="application/pdf",
            file_size=1,
            uploaded_by="gold",
            status="distributing",
        )
        PacketDistribution.objects.create(packet=unfinished, team=Team.objects.get(team_number=1))

        migration.complete_finished_packets(apps, None)

        self.packet.refresh_from_db()
        unfinished.refresh_from_db()
        self.assertEqual((self.packet.status, unfinished.status), ("completed", "distributing"))
