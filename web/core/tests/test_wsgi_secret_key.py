"""The web server refuses to start without a fixed secret key."""

import importlib
import sys
from unittest.mock import patch

import pytest
from django.core.exceptions import ImproperlyConfigured


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

    with pytest.raises(ImproperlyConfigured, match="DJANGO_SECRET_KEY"):
        _import_wsgi()


def test_wsgi_loads_with_a_key(monkeypatch):
    monkeypatch.setenv("DJANGO_SECRET_KEY", "a-real-key-for-this-test")

    assert _import_wsgi().application is not None
