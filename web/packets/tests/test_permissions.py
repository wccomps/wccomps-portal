"""Permission tests for packets views."""

import pytest
from django.test import Client
from django.urls import reverse

pytestmark = pytest.mark.django_db


@pytest.fixture
def team_1(db, blue_team):
    """Create test team 1 for blue_team_user."""
    return blue_team


class TestTeamPacketPermissions:
    """Team packet view requires blue_team (which includes gold_team via hierarchy)."""

    def test_unauthenticated_redirects(self, unauthenticated_client):
        response = unauthenticated_client.get(reverse("team_packet"))
        assert response.status_code == 302

    @pytest.mark.parametrize(
        "user_fixture",
        ["red_team_user", "white_team_user", "orange_team_user", "ticketing_support_user"],
    )
    def test_unauthorized_roles_denied(self, user_fixture, request):
        user = request.getfixturevalue(user_fixture)
        client = Client()
        client.force_login(user)
        response = client.get(reverse("team_packet"))
        assert response.status_code == 403, f"{user_fixture} should be denied"

    @pytest.mark.parametrize("user_fixture", ["blue_team_user", "gold_team_user", "admin_user"])
    def test_authorized_roles_allowed(self, user_fixture, request, team_1):
        user = request.getfixturevalue(user_fixture)
        client = Client()
        client.force_login(user)
        response = client.get(reverse("team_packet"))
        assert response.status_code == 200, f"{user_fixture} should have access"


class TestPacketsManagementPermissions:
    """Packets management requires gold_team."""

    def test_unauthenticated_redirects(self, unauthenticated_client):
        response = unauthenticated_client.get(reverse("packets_list"))
        assert response.status_code == 302

    @pytest.mark.parametrize(
        "user_fixture",
        ["blue_team_user", "red_team_user", "white_team_user", "orange_team_user", "ticketing_support_user"],
    )
    def test_unauthorized_roles_denied(self, user_fixture, request):
        user = request.getfixturevalue(user_fixture)
        client = Client()
        client.force_login(user)
        response = client.get(reverse("packets_list"))
        assert response.status_code == 403, f"{user_fixture} should be denied"

    @pytest.mark.parametrize("user_fixture", ["gold_team_user", "admin_user"])
    def test_authorized_roles_allowed(self, user_fixture, request):
        user = request.getfixturevalue(user_fixture)
        client = Client()
        client.force_login(user)
        response = client.get(reverse("packets_list"))
        assert response.status_code == 200, f"{user_fixture} should have access"


class TestPacketDownload:
    """A team downloads only packets distributed to it; gold_team downloads any web-enabled packet."""

    @pytest.fixture
    def packet(self, db, current_event):
        from packets.models import Packet

        return Packet.objects.create(
            event=current_event,
            title="Rules",
            file_data=b"%PDF",
            filename="rules.pdf",
            mime_type="application/pdf",
            file_size=4,
            status="completed",
            uploaded_by="gold",
        )

    def _get(self, user, packet):
        client = Client()
        client.force_login(user)
        return client.get(reverse("download_packet", args=[packet.id]))

    def test_team_with_distribution_downloads_and_is_counted(self, blue_team_user, team_1, packet):
        from packets.models import PacketDistribution

        distribution = PacketDistribution.objects.create(packet=packet, team=team_1)
        response = self._get(blue_team_user, packet)

        assert response.status_code == 200
        assert response.content == b"%PDF"
        distribution.refresh_from_db()
        assert distribution.download_count == 1

    def test_team_without_distribution_gets_404_and_no_row(self, blue_team_user, team_1, packet):
        from packets.models import PacketDistribution

        assert self._get(blue_team_user, packet).status_code == 404
        assert not PacketDistribution.objects.exists()

    def test_team_with_web_access_disabled_gets_404(self, blue_team_user, team_1, packet):
        from packets.models import PacketDistribution

        PacketDistribution.objects.create(packet=packet, team=team_1, web_access_enabled=False)
        assert self._get(blue_team_user, packet).status_code == 404

    def test_gold_team_downloads_without_distribution(self, gold_team_user, packet):
        assert self._get(gold_team_user, packet).status_code == 200

    def test_other_roles_denied(self, red_team_user, packet):
        assert self._get(red_team_user, packet).status_code == 403
