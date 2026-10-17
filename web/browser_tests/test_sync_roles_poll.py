"""The role sync page polls its task's status; an error answer (the task gone, a refusal) stops the polling and
shows the reason, instead of polling every second for good."""

import pytest
from django.urls import reverse

from core.tests.roles import role_user

from .conftest import create_session_context

pytestmark = [pytest.mark.browser, pytest.mark.django_db(transaction=True)]


def test_a_missing_task_stops_the_poll_and_says_so(live_server, pw_browser):
    from core.models import DiscordTask

    context = create_session_context(pw_browser, live_server, role_user("admin"))
    page = context.new_page()
    page.on("dialog", lambda dialog: dialog.accept())
    polls: list[str] = []
    page.on("request", lambda r: polls.append(r.url) if "/ops/admin/task/" in r.url else None)
    try:
        page.goto(live_server.url + reverse("admin_sync_roles"))
        page.get_by_role("button", name="Preview Changes (Dry Run)").click()
        page.wait_for_timeout(1500)  # the task is queued and polled (no bot runs here, so it stays pending)
        assert polls
        DiscordTask.objects.filter(task_type="sync_roles").delete()
        page.get_by_text("Not found").wait_for()
        seen = len(polls)
        page.wait_for_timeout(2500)
        assert len(polls) == seen, "kept polling after the task was gone"
    finally:
        context.close()
