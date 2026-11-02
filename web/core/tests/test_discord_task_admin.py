"""The DiscordTask admin's actions."""

import pytest
from django.contrib.admin.sites import site
from django.test import RequestFactory

from core.models import DiscordTask

pytestmark = pytest.mark.django_db


def test_retrying_failed_tasks_leaves_a_scrubbed_start_broadcast_failed(admin_user):
    """Its placeholder would go to every team."""
    from django.contrib.messages.storage.fallback import FallbackStorage
    from registration.services import START_MESSAGE_SCRUBBED

    scrubbed = DiscordTask.objects.create(
        task_type="broadcast_message",
        payload={"target": "all-teams", "message": START_MESSAGE_SCRUBBED, "sender": "Black Team"},
        status="failed",
    )
    other = DiscordTask.objects.create(task_type="log_to_channel", payload={"message": "hi"}, status="failed")
    request = RequestFactory().post("/")
    request.user = admin_user
    request.session = {}
    request._messages = FallbackStorage(request)

    site._registry[DiscordTask].retry_failed_tasks(request, DiscordTask.objects.all())

    statuses = dict(DiscordTask.objects.values_list("pk", "status"))
    assert statuses == {scrubbed.pk: "failed", other.pk: "pending"}
    assert "1 start broadcasts scrubbed when the event ended are not retried" in str(list(request._messages)[0])
