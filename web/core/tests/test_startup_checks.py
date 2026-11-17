"""Settings a server refuses (portal.required) and how settings.py reads yes/no values and request sizes."""

import runpy
from pathlib import Path

import portal
import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import Client
from django.urls import reverse
from portal.required import HTTPS_SETTINGS, plain_http_settings, refuse_to_start, secret_key_problem

SETTINGS_PATH = str(Path(portal.__file__).parent / "settings.py")
BOOLEANS = {
    "DJANGO_DEBUG": ("DEBUG", False),
    "TRUST_CF_CONNECTING_IP": ("TRUST_CF_CONNECTING_IP", False),
    "PROXMOX_VERIFY_SSL": ("PROXMOX_VERIFY_SSL", True),
    "EMAIL_USE_TLS": ("use_tls", True),
    "EMAIL_USE_SSL": ("use_ssl", False),
}


def _read(monkeypatch, name: str, value: str | None) -> object:
    for other in BOOLEANS:
        monkeypatch.delenv(other, raising=False)
    monkeypatch.delenv("EMAIL_BACKEND", raising=False)
    if value is not None:
        monkeypatch.setenv(name, value)
    loaded = runpy.run_path(SETTINGS_PATH)
    key = BOOLEANS[name][0]
    return loaded["MAILERS"]["default"]["OPTIONS"][key] if name.startswith("EMAIL_") else loaded[key]


@pytest.mark.parametrize("name", BOOLEANS)
@pytest.mark.parametrize(
    ("value", "expected"),
    [("true", True), ("TRUE", True), (" Yes ", True), ("on", True), ("1", True)]
    + [("false", False), ("False", False), ("no", False), ("OFF", False), ("0", False)],
)
def test_each_flag_reads_any_spelling(monkeypatch, name, value, expected):
    """Lowercase true once turned PROXMOX_VERIFY_SSL and EMAIL_USE_TLS off: only the exact text True counted."""
    assert _read(monkeypatch, name, value) is expected


@pytest.mark.parametrize("name", BOOLEANS)
@pytest.mark.parametrize("value", [None, "", "  "])
def test_an_unset_flag_is_its_default(monkeypatch, name, value):
    assert _read(monkeypatch, name, value) is BOOLEANS[name][1]


@pytest.mark.parametrize("name", BOOLEANS)
@pytest.mark.parametrize("value", ["ture", "enabled", "2", "y"])
def test_an_unreadable_flag_is_refused(monkeypatch, name, value):
    with pytest.raises(ImproperlyConfigured, match=name):
        _read(monkeypatch, name, value)


@pytest.mark.parametrize("key", ["x" * 45 + "abcd", "abcd" * 13])
def test_a_weak_secret_key_is_refused(monkeypatch, key):
    monkeypatch.setenv("DJANGO_SECRET_KEY", key)
    assert "too weak" in (secret_key_problem() or "")


def test_a_key_of_fifty_with_five_distinct_characters_passes(monkeypatch):
    monkeypatch.setenv("DJANGO_SECRET_KEY", "abcde" * 10)
    assert secret_key_problem() is None


@pytest.fixture
def services(settings):
    """Every credentialed service configured over https, with no http allowed."""
    settings.AUTHENTIK_URL = "https://auth.example.org"
    settings.AUTHENTIK_OIDC_URL = "https://auth.example.org/application/o/portal/"
    settings.NEXTCLOUD_URL = "https://cloud.example.org"
    settings.QUOTIENT_API_URL = "https://scoring.example.org"
    settings.PROXMOX_API_URL = "https://pve1.example.org:8006,https://pve2.example.org:8006"
    settings.AWEFUL_PROXY_URL = "https://proxy.example.org"
    settings.AWEFUL_PROXY_TOKEN = "token"
    settings.ALLOW_HTTP_URLS = []
    return settings


def test_https_services_pass(services):
    assert plain_http_settings() == []


@pytest.mark.parametrize("name", HTTPS_SETTINGS)
@pytest.mark.parametrize("url", ["http://svc.example.org", " HTTP://svc.example.org", "svc.example.org"])
def test_each_plain_http_service_is_refused(services, name, url):
    setattr(services, name, url)
    assert plain_http_settings() == [f"{name} must be https: requests to it carry a credential"]


def test_one_plain_proxmox_host_among_several_is_refused(services):
    services.PROXMOX_API_URL = "https://pve1.example.org:8006, http://pve2.example.org:8006"
    assert plain_http_settings() == ["PROXMOX_API_URL must be https: requests to it carry a credential"]


def test_the_message_leaves_the_url_out(services):
    services.NEXTCLOUD_URL = "http://admin:secret@cloud.example.org"
    assert "secret" not in "".join(plain_http_settings())


def test_an_unset_service_is_not_checked(services):
    services.NEXTCLOUD_URL = services.QUOTIENT_API_URL = services.PROXMOX_API_URL = ""
    assert plain_http_settings() == []


def test_aweful_proxy_without_a_token_is_not_checked(services):
    services.AWEFUL_PROXY_URL, services.AWEFUL_PROXY_TOKEN = "http://proxy.example.org", " "
    assert plain_http_settings() == []


def test_allow_http_urls_lets_only_the_named_setting_be_plain(services):
    """WRCCDC's aweful-proxy is an in-cluster http Service, so production names it."""
    services.AWEFUL_PROXY_URL = "http://aweful-proxy.aweful-proxy.svc.cluster.local:8080"
    services.NEXTCLOUD_URL = "http://cloud.example.org"
    services.ALLOW_HTTP_URLS = ["AWEFUL_PROXY_URL"]
    assert plain_http_settings() == ["NEXTCLOUD_URL must be https: requests to it carry a credential"]


def test_allow_http_urls_naming_another_setting_is_refused(services):
    services.ALLOW_HTTP_URLS = ["BASE_URL"]
    assert plain_http_settings() == ["ALLOW_HTTP_URLS names BASE_URL, which isn't checked"]


def test_allow_http_urls_is_read_as_a_list(monkeypatch):
    monkeypatch.setenv("ALLOW_HTTP_URLS", " AWEFUL_PROXY_URL , QUOTIENT_API_URL,")
    assert runpy.run_path(SETTINGS_PATH)["ALLOW_HTTP_URLS"] == ["AWEFUL_PROXY_URL", "QUOTIENT_API_URL"]


def test_every_problem_is_named_at_once(monkeypatch, services):
    monkeypatch.setenv("DJANGO_SECRET_KEY", "changeme")
    services.ORG_NAME = ""
    services.QUOTIENT_API_URL = "http://scoring.example.org"
    with pytest.raises(ImproperlyConfigured) as refused:
        refuse_to_start("the test")
    message = str(refused.value)
    assert message.startswith("ORG_NAME not set; DJANGO_SECRET_KEY is too weak")
    assert message.endswith(
        "; QUOTIENT_API_URL must be https: requests to it carry a credential; the test refuses to start"
    )


@pytest.mark.django_db
def test_form_data_past_256_kb_is_refused_before_the_view():
    """Without the cap, Django read 2.5 MB of comment text into one form field."""
    client = Client(enforce_csrf_checks=True)
    url = reverse("registration_register")
    # Under the cap the request gets as far as the CSRF check (403); over it, reading the form fails first (400)
    assert client.post(url, {"notes": "x" * (200 * 1024)}).status_code == 403
    assert client.post(url, {"notes": "x" * (300 * 1024)}).status_code == 400
