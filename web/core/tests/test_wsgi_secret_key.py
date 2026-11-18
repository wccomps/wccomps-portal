"""The web server refuses to start without a strong fixed secret key, a required setting, or https service URLs."""

import importlib
import sys
from unittest.mock import patch

import pytest
from django.core.exceptions import ImproperlyConfigured

STRONG_KEY = "a-real-key-for-this-test-0123456789-abcdefghijklmnopqrstuvwxyz"


def _import_wsgi():
    sys.modules.pop("portal.wsgi", None)
    # Warming (tested in test_warm_start) would freeze the test process's objects
    with patch("portal.warmup.warm"):
        return importlib.import_module("portal.wsgi")


@pytest.mark.parametrize("value", [None, "", "your_random_secret_key_here"])
def test_wsgi_refuses_a_missing_or_placeholder_key(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("DJANGO_SECRET_KEY", raising=False)
    else:
        monkeypatch.setenv("DJANGO_SECRET_KEY", value)

    with pytest.raises(ImproperlyConfigured, match="DJANGO_SECRET_KEY is not set"):
        _import_wsgi()


@pytest.mark.parametrize(
    "value",
    ["changeme", ("abcdefghij" * 5)[:49], "abcd" * 20],  # short; one short of 50; long but four distinct characters
)
def test_wsgi_refuses_a_weak_key(monkeypatch, value):
    monkeypatch.setenv("DJANGO_SECRET_KEY", value)

    with pytest.raises(ImproperlyConfigured, match="DJANGO_SECRET_KEY is too weak"):
        _import_wsgi()


def test_wsgi_loads_with_a_strong_key(monkeypatch):
    monkeypatch.setenv("DJANGO_SECRET_KEY", STRONG_KEY)

    assert _import_wsgi().application is not None


def test_wsgi_refuses_missing_required_settings(monkeypatch, settings):
    monkeypatch.setenv("DJANGO_SECRET_KEY", STRONG_KEY)
    settings.ORG_NAME = settings.DEFAULT_FROM_EMAIL = ""

    with pytest.raises(ImproperlyConfigured, match="^DEFAULT_FROM_EMAIL, ORG_NAME not set; the web server refuses"):
        _import_wsgi()


def test_wsgi_refuses_a_plain_http_authentik(monkeypatch, settings):
    monkeypatch.setenv("DJANGO_SECRET_KEY", STRONG_KEY)
    settings.AUTHENTIK_URL = "http://auth.example.org"

    with pytest.raises(ImproperlyConfigured, match="^AUTHENTIK_URL must be https"):
        _import_wsgi()
