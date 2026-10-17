import pytest
from django.contrib.auth.models import User
from django.test import Client

from core.models import UserGroups
from orange_team.models import OrangeCheckIn

pytestmark = pytest.mark.django_db


class TestToggleCheckIn:
    def test_check_in(self) -> None:
        user = User.objects.create_user(username="orange1", password="test")
        UserGroups.objects.create(user=user, authentik_id="o1", groups=["WCComps_OrangeTeam"])
        client = Client()
        client.force_login(User.objects.get(username="orange1"))
        response = client.post("/orange-team/check-in/")
        assert response.status_code == 302
        assert OrangeCheckIn.objects.filter(user=user, is_active=True).exists()

    def test_check_out(self) -> None:
        user = User.objects.create_user(username="orange1", password="test")
        UserGroups.objects.create(user=user, authentik_id="o1", groups=["WCComps_OrangeTeam"])
        OrangeCheckIn.objects.create(user=user)
        client = Client()
        client.force_login(User.objects.get(username="orange1"))
        response = client.post("/orange-team/check-in/")
        assert response.status_code == 302
        checkin = OrangeCheckIn.objects.get(user=user)
        assert not checkin.is_active
        assert checkin.checked_out_at is not None

    def test_get_request_redirects(self) -> None:
        user = User.objects.create_user(username="orange1", password="test")
        UserGroups.objects.create(user=user, authentik_id="o1", groups=["WCComps_OrangeTeam"])
        client = Client()
        client.force_login(User.objects.get(username="orange1"))
        response = client.get("/orange-team/check-in/")
        assert response.status_code == 302


class TestAdminToggleCheckIn:
    def test_lead_can_check_in_other(self) -> None:
        lead = User.objects.create_user(username="gold1", password="test")
        UserGroups.objects.create(user=lead, authentik_id="g1", groups=["WCComps_GoldTeam"])
        orange = User.objects.create_user(username="orange1")
        client = Client()
        client.force_login(User.objects.get(username="gold1"))
        response = client.post(f"/orange-team/check-in/{orange.pk}/")
        assert response.status_code == 302
        assert OrangeCheckIn.objects.filter(user=orange, is_active=True).exists()

    def test_non_lead_denied(self) -> None:
        user = User.objects.create_user(username="orange1", password="test")
        UserGroups.objects.create(user=user, authentik_id="o1", groups=["WCComps_OrangeTeam"])
        target = User.objects.create_user(username="orange2")
        client = Client()
        client.force_login(User.objects.get(username="orange1"))
        response = client.post(f"/orange-team/check-in/{target.pk}/")
        assert response.status_code == 403
        assert not OrangeCheckIn.objects.filter(user=target).exists()
