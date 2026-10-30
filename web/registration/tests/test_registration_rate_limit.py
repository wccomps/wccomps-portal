"""Registration rate limit counts per real client, shared across workers (security review, Medium)."""

from ipaddress import ip_address

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
    assert client_ip(request) == ip_address("198.51.100.20")


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
    assert client_ip(request) == ip_address("203.0.113.7")


@pytest.mark.parametrize("header", ["", "not-an-ip", "203.0.113.7, 10.0.0.1"])
def test_client_ip_falls_back_on_missing_or_malformed_header(header):
    request = RequestFactory().get("/", REMOTE_ADDR=PROXY, HTTP_CF_CONNECTING_IP=header)
    assert client_ip(request) == ip_address(PROXY)


def test_ipv6_client_supported():
    request = RequestFactory().get("/", REMOTE_ADDR=PROXY, HTTP_CF_CONNECTING_IP="2001:db8::5")
    assert client_ip(request) == ip_address("2001:db8::5")


@pytest.mark.parametrize("header", ['fe80::1%eth0" 200 fake', "fe80::1%x", "2001:db8::5%[a b]"])
def test_an_address_with_a_scope_id_is_refused(header):
    request = RequestFactory().get("/", REMOTE_ADDR=PROXY, HTTP_CF_CONNECTING_IP=header)
    assert client_ip(request) == ip_address(PROXY)


@override_settings(TRUST_CF_CONNECTING_IP=False)
@pytest.mark.parametrize(
    ("remote", "address"),
    [("::ffff:198.51.100.20", "198.51.100.20"), ("fe80::1%eth0", "fe80::1"), ("", "0.0.0.0")],  # noqa: S104 - an address value
)
def test_the_connecting_address_is_parsed_too(remote, address):
    assert client_ip(RequestFactory().get("/", REMOTE_ADDR=remote)) == ip_address(address)


def test_an_ipv4_mapped_address_is_its_ipv4():
    request = RequestFactory().get("/", REMOTE_ADDR=PROXY, HTTP_CF_CONNECTING_IP="::ffff:203.0.113.7")
    assert client_ip(request) == ip_address("203.0.113.7")


def _register(ip: str):
    return Client().post(reverse("registration_register"), {}, REMOTE_ADDR=PROXY, HTTP_CF_CONNECTING_IP=ip)


def test_limit_is_per_client_not_per_proxy():
    for _ in range(10):
        RegistrationRateLimit.objects.create(ip="203.0.113.7")

    blocked = _register("203.0.113.7")
    other = _register("198.51.100.9")  # same proxy REMOTE_ADDR, different real client

    assert blocked.status_code == 429
    assert other.status_code != 429


def test_ipv6_clients_in_one_64_share_a_bucket():
    for host in range(10):
        RegistrationRateLimit.record(ip_address(f"2001:db8:1:2::{host + 1:x}"))

    assert RegistrationRateLimit.over_limit(ip_address("2001:db8:1:2:ffff:ffff:ffff:ffff"))
    assert not RegistrationRateLimit.over_limit(ip_address("2001:db8:1:3::1"))
    assert set(RegistrationRateLimit.objects.values_list("ip", flat=True)) == {"2001:db8:1:2::"}


def test_ipv6_clients_share_a_larger_limit_per_48():
    for subnet in range(RegistrationRateLimit.NETWORK_LIMIT):
        RegistrationRateLimit.record(ip_address(f"2001:db8:1:{subnet:x}::1"))

    assert RegistrationRateLimit.over_limit(ip_address("2001:db8:1:ffff::1"))
    assert not RegistrationRateLimit.over_limit(ip_address("2001:db8:2::1"))


def test_an_ipv6_client_is_stored_as_its_longest_tier_whatever_the_tier_order(monkeypatch):
    tiers = RegistrationRateLimit.TIERS
    monkeypatch.setattr(RegistrationRateLimit, "TIERS", {**tiers, 6: tuple(reversed(tiers[6]))})

    RegistrationRateLimit.record(ip_address("2001:db8:1:2:3:4:5:6"))

    assert list(RegistrationRateLimit.objects.values_list("ip", flat=True)) == ["2001:db8:1:2::"]


def test_ipv4_clients_keep_their_own_bucket():
    for _ in range(10):
        RegistrationRateLimit.record(ip_address("203.0.113.7"))

    assert RegistrationRateLimit.over_limit(ip_address("203.0.113.7"))
    assert not RegistrationRateLimit.over_limit(ip_address("203.0.113.8"))


def test_counter_is_shared_database_state_not_process_memory():
    """Every gunicorn worker sees the same count (the old LocMemCache counter was per worker)."""
    RegistrationRateLimit.objects.bulk_create([RegistrationRateLimit(ip="203.0.113.7") for _ in range(10)])

    assert RegistrationRateLimit.over_limit(ip_address("203.0.113.7"))
    assert not RegistrationRateLimit.over_limit(ip_address("198.51.100.9"))


def test_old_attempts_do_not_count():
    from datetime import timedelta

    from django.utils import timezone

    for _ in range(10):
        row = RegistrationRateLimit.objects.create(ip="203.0.113.7")
        RegistrationRateLimit.objects.filter(pk=row.pk).update(attempted_at=timezone.now() - timedelta(hours=2))

    assert not RegistrationRateLimit.over_limit(ip_address("203.0.113.7"))


def _race(first: str, second: str) -> list[bool]:
    """Two workers checking and counting a submission at once, from first and second; whether each was counted."""
    import threading
    import time

    from django.db import connection

    counted: list[bool] = []

    def submit(address: str) -> None:
        try:
            with RegistrationRateLimit.held(ip_address(address)) as over:
                if not over:
                    time.sleep(0.5)  # the window another worker would race into
                    RegistrationRateLimit.record(ip_address(address))
                counted.append(not over)
        finally:
            connection.close()

    workers = [threading.Thread(target=submit, args=(address,)) for address in (first, second)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=30)
    return counted


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("address", ["203.0.113.7", "2001:db8:1:2::1"])
def test_concurrent_submissions_cannot_both_take_the_last_place(address):
    for _ in range(RegistrationRateLimit.LIMIT - 1):
        RegistrationRateLimit.record(ip_address(address))

    assert sorted(_race(address, address)) == [False, True]
    assert RegistrationRateLimit.objects.count() == RegistrationRateLimit.LIMIT


@pytest.mark.django_db(transaction=True)
def test_two_64s_in_one_48_cannot_both_take_its_last_place():
    for subnet in range(RegistrationRateLimit.NETWORK_LIMIT - 1):
        RegistrationRateLimit.record(ip_address(f"2001:db8:1:{subnet:x}::1"))

    assert sorted(_race("2001:db8:1:fff0::1", "2001:db8:1:fff1::1")) == [False, True]
    assert RegistrationRateLimit.objects.count() == RegistrationRateLimit.NETWORK_LIMIT
