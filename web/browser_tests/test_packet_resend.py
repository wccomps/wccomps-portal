"""A packet distribution row's Resend posts that row's email fields, which sit in other cells of the row."""

from urllib.parse import parse_qs

import pytest
from django.urls import reverse

from .conftest import _create_role_user, create_session_context
from .table_data import seed_table_data

pytestmark = [pytest.mark.browser, pytest.mark.django_db(transaction=True)]


def test_resend_posts_the_rows_emails(live_server, pw_browser):
    user = _create_role_user("admin", None)
    data = seed_table_data(user)
    context = create_session_context(pw_browser, live_server, user)
    page = context.new_page()
    posted = []
    page.route("**/resend/**", lambda route: (posted.append(route.request.post_data), route.abort()))
    try:
        page.goto(f"{live_server.url}{reverse('packet_detail', kwargs={'packet_id': data['packet'].pk})}")
        page.get_by_label("Primary email for Team 1").fill("ops@example.edu")
        page.get_by_label("Secondary email for Team 1").fill("second@example.edu")
        page.get_by_role("button", name="Resend").first.click()
        page.wait_for_timeout(500)
        fields = parse_qs(posted[0])
        assert fields["primary_email"] == ["ops@example.edu"]
        assert fields["secondary_email"] == ["second@example.edu"]
    finally:
        context.close()
