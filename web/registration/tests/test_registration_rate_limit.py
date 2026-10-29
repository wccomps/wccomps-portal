"""Registration rate limit counts per real client, shared across workers (security review, Medium)."""

import pytest
from django.test import Client, RequestFactory, override_settings
from django.urls import reverse

from core.utils import client_ip
from registration.models import RegistrationRateLimit

pytestmark = pytest.mark.django_db

PROXY = "10.42.3.17"  # REMOTE_ADDR in production is the proxy in front of the app, never the client


@pytest.fixture(autouse=True)
def behind_cloudflare(settings):
    settings.TRUST_CF_CONNECTING_IP = True


@override_settings(TRUST_CF_CONNECTING_IP=False)
def test_client_ip_ignores_cloudflare_header_unless_trusted():
    request = RequestFactory().get("/", REMOTE_ADDR="198.51.100.20", HTTP_CF_CONNECTING_IP="203.0.113.7")
    assert client_ip(request) == "198.51.100.20"


@override_settings(TRUST_CF_CONNECTING_IP=False)
def test_an_ignored_header_warns_once_per_process(caplog):
    from core.utils import _warn_untrusted_cf_header

    _warn_untrusted_cf_header.cache_clear()
    request = RequestFactory().get("/", REMOTE_ADDR="198.51.100.20", HTTP_CF_CONNECTING_IP="203.0.113.7")
    with caplog.at_level("WARNING", logger="core.utils"):
        client_ip(request)
        client_ip(request)

    warnings = [r for r in caplog.records if "TRUST_CF_CONNECTING_IP" in r.getMessage()]
    assert len(warnings) == 1
    assert warnings[0].levelname == "WARNING"


@override_settings(TRUST_CF_CONNECTING_IP=False)
def test_no_header_no_warning(caplog):
    from core.utils import _warn_untrusted_cf_header

    _warn_untrusted_cf_header.cache_clear()
    with caplog.at_level("WARNING", logger="core.utils"):
        client_ip(RequestFactory().get("/", REMOTE_ADDR="198.51.100.20"))

    assert not [r for r in caplog.records if "TRUST_CF_CONNECTING_IP" in r.getMessage()]


@pytest.mark.parametrize(
    ("value", "trusted"),
    [("True", True), ("true", True), (" TRUE ", True), ("1", True), ("Yes", True), ("", False), ("0", False)],
)
def test_the_flag_is_read_case_insensitively(monkeypatch, value, trusted):
    import runpy
    from pathlib import Path

    import portal.settings

    monkeypatch.setenv("TRUST_CF_CONNECTING_IP", value)
    assert runpy.run_path(str(Path(portal.settings.__file__)))["TRUST_CF_CONNECTING_IP"] is trusted


@override_settings(TRUST_CF_CONNECTING_IP=False)
def test_untrusted_header_cannot_dodge_the_rate_limit():
    for _ in range(10):
        RegistrationRateLimit.objects.create(ip="198.51.100.20")

    response = Client().post(
        reverse("registration_register"), {}, REMOTE_ADDR="198.51.100.20", HTTP_CF_CONNECTING_IP="203.0.113.99"
    )

    assert response.status_code == 429


def test_client_ip_prefers_cloudflare_header():
    request = RequestFactory().get("/", REMOTE_ADDR=PROXY, HTTP_CF_CONNECTING_IP="203.0.113.7")
    assert client_ip(request) == "203.0.113.7"


@pytest.mark.parametrize("header", ["", "not-an-ip", "203.0.113.7, 10.0.0.1"])
def test_client_ip_falls_back_on_missing_or_malformed_header(header):
    request = RequestFactory().get("/", REMOTE_ADDR=PROXY, HTTP_CF_CONNECTING_IP=header)
    assert client_ip(request) == PROXY


def test_ipv6_client_supported():
    request = RequestFactory().get("/", REMOTE_ADDR=PROXY, HTTP_CF_CONNECTING_IP="2001:db8::5")
    assert client_ip(request) == "2001:db8::5"


@pytest.mark.parametrize("header", ['fe80::1%eth0" 200 fake', "fe80::1%x", "2001:db8::5%[a b]"])
def test_an_address_with_a_scope_id_is_refused(header):
    request = RequestFactory().get("/", REMOTE_ADDR=PROXY, HTTP_CF_CONNECTING_IP=header)
    assert client_ip(request) == PROXY


def test_an_ipv4_mapped_address_is_its_ipv4():
    request = RequestFactory().get("/", REMOTE_ADDR=PROXY, HTTP_CF_CONNECTING_IP="::ffff:203.0.113.7")
    assert client_ip(request) == "203.0.113.7"


def _register(ip: str):
    return Client().post(reverse("registration_register"), {}, REMOTE_ADDR=PROXY, HTTP_CF_CONNECTING_IP=ip)


def test_limit_is_per_client_not_per_proxy():
    for _ in range(10):
        RegistrationRateLimit.objects.create(ip="203.0.113.7")

    blocked = _register("203.0.113.7")
    other = _register("198.51.100.9")  # same proxy REMOTE_ADDR, different real client

    assert blocked.status_code == 429
    assert other.status_code != 429


def test_counter_is_shared_database_state_not_process_memory():
    """Every gunicorn worker sees the same count (the old LocMemCache counter was per worker)."""
    RegistrationRateLimit.objects.bulk_create([RegistrationRateLimit(ip="203.0.113.7") for _ in range(10)])

    assert RegistrationRateLimit.over_limit("203.0.113.7")
    assert not RegistrationRateLimit.over_limit("198.51.100.9")


def test_old_attempts_do_not_count():
    from datetime import timedelta

    from django.utils import timezone

    for _ in range(10):
        row = RegistrationRateLimit.objects.create(ip="203.0.113.7")
        RegistrationRateLimit.objects.filter(pk=row.pk).update(attempted_at=timezone.now() - timedelta(hours=2))

    assert not RegistrationRateLimit.over_limit("203.0.113.7")
