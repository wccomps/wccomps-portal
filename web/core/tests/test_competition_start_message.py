"""The start message: saved on the competition page, checked for readiness, sent by run_competition."""

import pytest
from django.test import Client
from django.urls import reverse

from core.admin_views.readiness import _check_start_message
from core.models import AuditLog, CompetitionConfig

pytestmark = pytest.mark.django_db

MESSAGE = "Default VM login\nadmin / Changeme-2026!"


@pytest.fixture
def admin_client(admin_user):
    client = Client()
    client.force_login(admin_user)
    return client


def _save(client, message):
    return client.post(reverse("admin_competition_action"), {"action": "set_start_message", "start_message": message})


def test_saving_stores_the_message_and_audits_only_its_length(admin_client):
    response = _save(admin_client, MESSAGE)

    assert response.json() == {"success": True, "message": "Start message saved"}
    assert CompetitionConfig.get_config().start_message == MESSAGE
    entry = AuditLog.objects.get(action="competition_start_message_updated")
    assert entry.details == {"length": len(MESSAGE)}


def test_saving_empty_clears_it(admin_client):
    CompetitionConfig.objects.update_or_create(pk=1, defaults={"start_message": MESSAGE})

    response = _save(admin_client, "")

    assert response.json()["message"] == "Start message cleared"
    assert CompetitionConfig.get_config().start_message == ""


def test_browser_line_endings_are_normalized_before_the_length_check(admin_client):
    """A textarea counts a newline as one character but submits it as CRLF."""
    message = "\n".join(["x" * 99] * 19)  # 1899 characters as the textarea counts them

    response = _save(admin_client, message.replace("\n", "\r\n"))

    assert response.json()["success"]
    assert CompetitionConfig.get_config().start_message == message


def test_message_too_long_for_discord_is_refused(admin_client):
    response = _save(admin_client, "x" * 1901)

    assert response.status_code == 400
    assert response.json()["error"] == "The start message must be at most 1900 characters"
    assert CompetitionConfig.get_config().start_message == ""


def test_page_loads_the_stored_message_as_json(admin_client):
    CompetitionConfig.objects.update_or_create(pk=1, defaults={"start_message": "</script><b>x</b>"})

    content = admin_client.get(reverse("admin_competition")).content.decode()

    assert '<script id="start-message-data" type="application/json">' in content
    assert "</script><b>x</b>" not in content


def test_readiness_warns_without_a_start_message():
    CompetitionConfig.objects.update_or_create(pk=1, defaults={"start_message": " "})
    assert _check_start_message()[0] == "warn"

    CompetitionConfig.objects.filter(pk=1).update(start_message=MESSAGE)
    assert _check_start_message()[0] == "pass"
