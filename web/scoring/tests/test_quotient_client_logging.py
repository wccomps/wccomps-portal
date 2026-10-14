"""Quotient being down is expected between competitions: log one line, not a traceback."""

import logging
from collections.abc import Callable

import httpx
import pytest
from quotient import client as client_module
from quotient.client import QuotientClient


@pytest.fixture(autouse=True)
def _quotient_settings(settings):
    settings.QUOTIENT_USERNAME = "admin"
    settings.QUOTIENT_PASSWORD = "secret"


def _client_with(monkeypatch, handler: Callable[[httpx.Request], httpx.Response]) -> QuotientClient:
    real_client = httpx.Client
    monkeypatch.setattr(client_module.httpx, "Client", lambda: real_client(transport=httpx.MockTransport(handler)))
    return QuotientClient(base_url="https://scoring.example")


def _tracebacks(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.exc_info]


def test_login_502_logs_single_warning_without_traceback(monkeypatch, caplog):
    client = _client_with(monkeypatch, lambda request: httpx.Response(502))

    with caplog.at_level(logging.DEBUG, logger="quotient.client"):
        assert client.get_infrastructure(force_refresh=True) is None

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "unavailable" in warnings[0].getMessage()
    assert "\n" not in warnings[0].getMessage()
    assert not _tracebacks(caplog)
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_connection_error_logs_single_warning_without_traceback(monkeypatch, caplog):
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = _client_with(monkeypatch, refuse)

    with caplog.at_level(logging.DEBUG, logger="quotient.client"):
        assert client.get_infrastructure(force_refresh=True) is None

    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1
    assert not _tracebacks(caplog)


def test_bad_credentials_keep_traceback(monkeypatch, caplog):
    """A 401 on login is misconfiguration, not downtime: keep the full error."""
    client = _client_with(monkeypatch, lambda request: httpx.Response(401))

    with caplog.at_level(logging.DEBUG, logger="quotient.client"):
        assert client.get_infrastructure(force_refresh=True) is None

    assert [r for r in caplog.records if r.levelno >= logging.ERROR and r.exc_info]


def test_endpoint_503_after_login_logs_single_warning(monkeypatch, caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/login":
            return httpx.Response(200, json={})
        return httpx.Response(503)

    client = _client_with(monkeypatch, handler)

    with caplog.at_level(logging.DEBUG, logger="quotient.client"):
        assert client.get_injects(force_refresh=True) is None

    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1
    assert not _tracebacks(caplog)
