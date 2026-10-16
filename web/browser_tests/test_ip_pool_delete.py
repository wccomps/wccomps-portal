"""Deleting an IP pool asks first, naming the pool as text whatever the name holds."""

import pytest
from django.urls import reverse

from core.tests.roles import role_user

from .conftest import create_session_context

pytestmark = [pytest.mark.browser, pytest.mark.django_db(transaction=True)]

NAME = "x');document.title='ran';('"


@pytest.mark.parametrize("accept", [True, False])
def test_delete_asks_with_the_pool_name_as_text(live_server, pw_browser, accept):
    from scoring.models import RedTeamIPPool

    user = role_user("red_team")
    pool = RedTeamIPPool.objects.create(name=NAME, ip_addresses="10.0.0.1", created_by=user)

    context = create_session_context(pw_browser, live_server, user)
    page = context.new_page()
    questions: list[str] = []

    def answer(dialog):
        questions.append(dialog.message)
        if accept:
            dialog.accept()
        else:
            dialog.dismiss()

    page.on("dialog", answer)
    try:
        page.goto(f"{live_server.url}{reverse('scoring:ip_pool_list')}")
        page.get_by_role("button", name="Delete").click()
        page.wait_for_load_state("networkidle")
        assert questions == [f"Delete pool '{NAME}'?"]
        assert page.title() != "ran"
        assert RedTeamIPPool.objects.filter(pk=pool.pk).exists() is not accept
    finally:
        context.close()
