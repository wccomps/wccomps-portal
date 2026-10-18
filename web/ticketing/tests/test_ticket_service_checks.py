"""Tests for Quotient service check debug info on service tickets (#172)."""

import json
import logging
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from django.core.cache import cache
from django.test import Client, override_settings
from django.urls import reverse
from quotient.client import QuotientClient, ServiceCheck, quotient_service_name

from core.tests.external_json import Answer, serving
from ticketing.models import Ticket, TicketCategory

pytestmark = pytest.mark.django_db


def _client(user):
    client = Client()
    client.force_login(user)
    return client


@pytest.fixture
def service_ticket(blue_team):
    category = TicketCategory.objects.get(pk=6)
    return Ticket.objects.create(
        team=blue_team,
        ticket_number="T001-001",
        category=category,
        title="DNS check failing",
        hostname="TREX",
        service_name="TREX:DNS",
    )


@pytest.mark.parametrize(
    ("service_name", "hostname", "expected"),
    [
        ("TREX:DNS", "", "TREX-DNS"),
        ("TREX:DNS", "TREX", "TREX-DNS"),
        ("TREX:TREX-DNS", "TREX", "TREX-DNS"),
        ("TREX-DNS", "TREX", "TREX-DNS"),
        ("DNS", "TREX", "TREX-DNS"),
        ("dns", "trex", "trex-dns"),
        (":DNS", "TREX", "TREX-DNS"),
        ("TREX:", "TREX", ""),
        (":", "", ""),
        ("HTTP", "", "HTTP"),
        ("", "TREX", ""),
        (".", "", ""),
        ("..", "", ""),
        ("../2/TREX-DNS", "", ""),
        ("2/TREX-DNS", "TREX", ""),
        ("TREX:../2/TREX-DNS", "", ""),
        ("DNS?team=2", "TREX", ""),
    ],
)
def test_quotient_service_name_formats_box_and_service(service_name, hostname, expected):
    assert quotient_service_name(service_name, hostname) == expected


def test_get_service_checks_parses_sorts_and_caches_results():
    cache.clear()
    payload = [
        {
            "Round": {"ID": 4, "StartTime": "2026-10-18T19:28:00Z"},
            "Result": True,
            "Debug": "record matched 192.168.220.12",
            "Error": "",
        },
        {
            "Round": {"ID": 5, "StartTime": "2026-10-18T19:30:00Z"},
            "Result": False,
            "Debug": "queried 10.100.11.12",
            "Error": "check timeout exceeded",
        },
    ]
    answers = [Answer(200, b"{}"), Answer(200, json.dumps(payload).encode())]
    with override_settings(QUOTIENT_USERNAME="admin", QUOTIENT_PASSWORD="secret"), serving(answers) as requests:
        qclient = QuotientClient(base_url="https://quotient.test")
        checks = qclient.get_service_checks(1, "TREX:DNS")
        cached = qclient.get_service_checks(1, "TREX:DNS")

    assert checks == [
        ServiceCheck(
            round_number=5,
            timestamp=datetime(2026, 10, 18, 19, 30, tzinfo=UTC),
            passed=False,
            debug="queried 10.100.11.12",
            error="check timeout exceeded",
        ),
        ServiceCheck(
            round_number=4,
            timestamp=datetime(2026, 10, 18, 19, 28, tzinfo=UTC),
            passed=True,
            debug="record matched 192.168.220.12",
            error="",
        ),
    ]
    assert cached == checks
    assert requests == ["https://quotient.test/api/login", "https://quotient.test/api/services/1/TREX-DNS"]


def test_get_service_checks_handles_go_null_slice_and_zero_timestamp():
    cache.clear()
    payload = [
        {
            "Round": {"ID": 1, "StartTime": "0001-01-01T00:00:00Z"},
            "Result": False,
            "Debug": "",
            "Error": "uninitialized",
        }
    ]
    answers = [
        Answer(200, b"{}"),
        Answer(200, b"null"),
        Answer(200, json.dumps(payload).encode()),
    ]
    with override_settings(QUOTIENT_USERNAME="admin", QUOTIENT_PASSWORD="secret"), serving(answers):
        qclient = QuotientClient(base_url="https://quotient.test")
        assert qclient.get_service_checks(1, "TREX:DNS", force_refresh=True) == []
        assert qclient.get_service_checks(1, "TREX:DNS", force_refresh=True) == [
            ServiceCheck(round_number=1, timestamp=None, passed=False, debug="", error="uninitialized")
        ]


def test_get_service_checks_never_logs_check_output_on_parse_error(caplog):
    cache.clear()
    secret_debug = "secret-internal-host.great.cretaceous:8443"
    malformed = [{"Round": {"ID": "not-an-int"}, "Result": False, "Debug": secret_debug, "Error": secret_debug}]
    answers = [Answer(200, b"{}"), Answer(200, json.dumps(malformed).encode())]
    with (
        override_settings(QUOTIENT_USERNAME="admin", QUOTIENT_PASSWORD="secret"),
        serving(answers),
        caplog.at_level(logging.DEBUG),
    ):
        result = QuotientClient(base_url="https://quotient.test").get_service_checks(1, "TREX:DNS", force_refresh=True)

    assert result is None
    assert secret_debug not in caplog.text


def test_ticket_detail_shows_service_check_section_only_to_staff(
    blue_team_user, ticketing_support_user, service_ticket
):
    url = reverse("ticket_detail", args=[service_ticket.ticket_number])
    checks_url = reverse("ticket_service_checks", args=[service_ticket.ticket_number])

    team_html = _client(blue_team_user).get(url).content.decode()
    assert "Service Check History" not in team_html
    assert checks_url not in team_html

    staff_html = _client(ticketing_support_user).get(url).content.decode()
    assert "Service Check History" in staff_html
    assert f'hx-get="{checks_url}"' in staff_html
    assert 'hx-trigger="load"' in staff_html


def test_ticket_detail_omits_service_check_section_without_service_name(ticketing_support_user, blue_team):
    category = TicketCategory.objects.get(pk=6)
    ticket = Ticket.objects.create(
        team=blue_team,
        ticket_number="T001-002",
        category=category,
        title="General question",
        service_name="",
    )
    url = reverse("ticket_detail", args=[ticket.ticket_number])
    staff_html = _client(ticketing_support_user).get(url).content.decode()
    assert "Service Check History" not in staff_html

    checks_html = (
        _client(ticketing_support_user)
        .get(reverse("ticket_service_checks", args=[ticket.ticket_number]))
        .content.decode()
    )
    assert "No service specified for this ticket." in checks_html


def test_non_staff_roles_cannot_fetch_service_checks_endpoint(
    blue_team_user, red_team_user, orange_team_user, white_team_user, gold_team_user, service_ticket
):
    url = reverse("ticket_service_checks", args=[service_ticket.ticket_number])
    mock_client = MagicMock()
    with patch("quotient.client.get_quotient_client", return_value=mock_client):
        anon_response = Client().get(url)
        assert anon_response.status_code == 302
        for user in (blue_team_user, red_team_user, orange_team_user, white_team_user, gold_team_user):
            response = _client(user).get(url)
            assert response.status_code == 403
            assert "TREX" not in response.content.decode()

    mock_client.get_service_checks.assert_not_called()


def test_service_checks_endpoint_shows_unreachable_message_when_quotient_down(ticketing_support_user, service_ticket):
    mock_client = MagicMock()
    mock_client.get_service_checks.return_value = None
    with patch("quotient.client.get_quotient_client", return_value=mock_client):
        response = _client(ticketing_support_user).get(
            reverse("ticket_service_checks", args=[service_ticket.ticket_number])
        )

    assert response.status_code == 200
    html = response.content.decode()
    assert "Quotient isn't reachable." in html
    assert "Refresh" in html


def test_service_checks_endpoint_renders_streak_last_passed_and_recent_10(ticketing_support_user, service_ticket):
    checks = [
        ServiceCheck(
            round_number=15 - i,
            timestamp=datetime(2026, 10, 18, 19, 30 - i, tzinfo=UTC),
            passed=False,
            debug=f"attempt {15 - i} debug",
            error="connection refused",
        )
        for i in range(12)
    ] + [
        ServiceCheck(
            round_number=3,
            timestamp=datetime(2026, 10, 18, 19, 15, tzinfo=UTC),
            passed=True,
            debug="200 OK",
            error="",
        )
    ]
    mock_client = MagicMock()
    mock_client.get_service_checks.return_value = checks
    with patch("quotient.client.get_quotient_client", return_value=mock_client):
        response = _client(ticketing_support_user).get(
            reverse("ticket_service_checks", args=[service_ticket.ticket_number]),
            {"refresh": "1"},
        )

    assert response.status_code == 200
    mock_client.get_service_checks.assert_called_once_with(
        service_ticket.team.team_number,
        "TREX-DNS",
        force_refresh=True,
    )
    html = response.content.decode()
    assert "Failing" in html
    assert "for 12 rounds" in html
    assert "Round 3 (2026-10-18 19:15:00)" in html
    assert "attempt 15 debug" in html
    assert "attempt 6 debug" in html
    assert "attempt 5 debug" not in html
    assert "connection refused" in html


def test_get_service_checks_sanitizes_control_chars_and_caps_untrusted_output():
    cache.clear()
    hostile = "ok\x00\x1b[31m\r\u202ehidden\tline\n" + ("A" * 20_000)
    payload = [
        {
            "Round": {"ID": i, "StartTime": "2026-10-18T19:30:00Z"},
            "Result": False,
            "Debug": hostile,
            "Error": hostile,
        }
        for i in range(1, 13)
    ]
    answers = [Answer(200, b"{}"), Answer(200, json.dumps(payload).encode())]
    with override_settings(QUOTIENT_USERNAME="admin", QUOTIENT_PASSWORD="secret"), serving(answers):
        checks = QuotientClient(base_url="https://quotient.test").get_service_checks(1, "TREX:DNS", force_refresh=True)

    assert checks is not None
    assert len(checks) == 12
    latest = checks[0]
    assert latest.round_number == 12
    assert "\x00" not in latest.debug
    assert "\x1b" not in latest.debug
    assert "\r" not in latest.debug
    assert "\u202e" not in latest.debug
    assert latest.debug.startswith("ok[31mhidden\tline\nAAAA")
    assert latest.debug.endswith("\n... [truncated]")
    assert len(latest.debug) == 16_384 + len("\n... [truncated]")
    assert checks[9].debug != ""
    assert checks[10].debug == ""
    assert checks[10].error == ""


def test_service_checks_endpoint_escapes_and_collapses_large_html_debug_output(ticketing_support_user, service_ticket):
    large_html = (
        "<!DOCTYPE html><html><body><script>alert('debug')</script>" + ("<p>line</p>\n" * 1000) + "</body></html>"
    )
    checks = [
        ServiceCheck(
            round_number=1,
            timestamp=None,
            passed=False,
            debug=large_html,
            error='<img src=x onerror="alert(1)">',
        )
    ]
    mock_client = MagicMock()
    mock_client.get_service_checks.return_value = checks
    with patch("quotient.client.get_quotient_client", return_value=mock_client):
        response = _client(ticketing_support_user).get(
            reverse("ticket_service_checks", args=[service_ticket.ticket_number])
        )

    html = response.content.decode()
    assert "<script>alert" not in html
    assert "&lt;script&gt;alert(&#x27;debug&#x27;)&lt;/script&gt;" in html
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" in html
    assert "<details" in html
    assert "max-h-300 overflow-y-auto" in html
    assert "hx-disable" in html
    assert "x-ignore" in html
    assert "Never" in html
