"""Tests for packet models."""

from datetime import timedelta

import pytest
from django.utils import timezone

from team.models import Team

from ..models import Packet, PacketDistribution


@pytest.fixture
def team(db):
    """Create test team."""
    return Team.objects.create(team_number=1, team_name="Test Team", authentik_group="WCComps_BlueTeam01")


@pytest.fixture
def packet(db):
    """Create test packet."""
    return Packet.objects.create(
        title="Test Packet",
        file_data=b"test file content",
        filename="test.pdf",
        mime_type="application/pdf",
        file_size=100,
        uploaded_by="testuser",
    )


@pytest.mark.django_db
class TestPacket:
    """Test Packet model."""

    def test_packet_creation(self, packet):
        """Test packet can be created."""
        assert packet.title == "Test Packet"
        assert packet.status == "draft"
        assert packet.send_via_email is True
        assert packet.web_access_enabled is True

    def test_is_ready_for_distribution(self, packet):
        """Test packet readiness check."""
        # Draft packet is ready
        assert packet.is_ready_for_distribution() is True

        # Distributing packet is not ready
        packet.status = "distributing"
        packet.save()
        assert packet.is_ready_for_distribution() is False

        # Completed packet is not ready
        packet.status = "completed"
        packet.save()
        assert packet.is_ready_for_distribution() is False

    def test_mark_as_distributing(self, packet):
        """Test marking packet as distributing."""
        packet.mark_as_distributing()
        assert packet.status == "distributing"
        assert packet.actual_distribution_time is not None

    def test_mark_as_completed(self, packet):
        """Test marking packet as completed."""
        packet.mark_as_completed()
        assert packet.status == "completed"


@pytest.mark.django_db
class TestPacketDistribution:
    """Test PacketDistribution model."""

    def test_distribution_creation(self, team, packet):
        """Test distribution can be created."""
        distribution = PacketDistribution.objects.create(packet=packet, team=team)
        assert distribution.email_status == "pending"
        assert distribution.download_count == 0
        assert distribution.web_access_enabled is True

    def test_mark_as_sent(self, team, packet):
        """Test marking distribution as sent."""
        distribution = PacketDistribution.objects.create(packet=packet, team=team)
        email = "test@example.com"
        distribution.mark_as_sent(email)
        assert distribution.email_status == "sent"
        assert distribution.email_sent_to == email
        assert distribution.email_sent_at is not None

    def test_mark_as_failed(self, team, packet):
        """Test marking distribution as failed."""
        distribution = PacketDistribution.objects.create(packet=packet, team=team)
        error = "SMTP connection failed"
        distribution.mark_as_failed(error)
        assert distribution.email_status == "failed"
        assert distribution.email_error_message == error

    def test_record_download(self, team, packet):
        """Test recording a download."""
        distribution = PacketDistribution.objects.create(packet=packet, team=team)
        username = "testuser"
        distribution.record_download(username)

        assert distribution.download_count == 1
        assert distribution.downloaded_by == username
        assert distribution.downloaded_at is not None
        assert distribution.last_downloaded_at is not None

        # Record second download
        distribution.record_download("testuser2")
        assert distribution.download_count == 2
        assert distribution.downloaded_by == "testuser2"

    def test_a_download_keeps_the_first_download_time_and_moves_the_last(self, team, packet):
        distribution = PacketDistribution.objects.create(packet=packet, team=team)
        first = timezone.now() - timedelta(hours=1)
        PacketDistribution.objects.filter(pk=distribution.pk).update(downloaded_at=first, updated_at=first)

        distribution.record_download("team01")

        assert distribution.downloaded_at == first
        assert distribution.last_downloaded_at > first
        assert distribution.updated_at == distribution.last_downloaded_at

    def test_a_failure_written_from_a_stale_row_keeps_a_download_recorded_since(self, team, packet):
        distribution = PacketDistribution.objects.create(packet=packet, team=team)
        PacketDistribution.objects.get(pk=distribution.pk).record_download("team01")

        distribution.mark_as_failed("bounced")

        distribution.refresh_from_db()
        assert (distribution.email_status, distribution.download_count, distribution.downloaded_by) == (
            "failed",
            1,
            "team01",
        )
