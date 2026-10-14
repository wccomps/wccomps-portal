"""The admin delete page's Yes and No buttons render the same height, side by side."""

import pytest

from .conftest import create_session_context

pytestmark = [pytest.mark.browser, pytest.mark.django_db(transaction=True)]


def test_delete_confirmation_buttons_match(live_server, pw_browser):
    from django.contrib.auth.models import User

    from core.models import UserGroups
    from registration.models import Season

    admin = User.objects.create(username="admindelete", is_staff=True, is_superuser=True)
    UserGroups.objects.update_or_create(user=admin, defaults={"groups": ["WCComps_Discord_Admin"], "authentik_id": "x"})
    season = Season.objects.create(name="Spare", year=2090)
    context = create_session_context(pw_browser, live_server, admin)
    page = context.new_page()
    try:
        page.goto(f"{live_server.url}/admin/registration/season/{season.pk}/delete/")
        yes = page.locator('.delete-confirmation form input[type="submit"]').bounding_box()
        no = page.locator(".delete-confirmation form .cancel-link").bounding_box()
        assert abs(yes["height"] - no["height"]) <= 2
        assert abs(yes["y"] - no["y"]) <= 2
    finally:
        context.close()
