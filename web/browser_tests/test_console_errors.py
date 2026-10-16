"""Test that all pages render without JS or CSP console errors for each role."""

import pytest

from core.tests.roles import role_user

from .conftest import (
    BROWSER_ROLES,
    create_session_context,
    make_test_data,
    resolve_url,
    visit_and_capture_errors,
)
from .page_registry import PAGES

pytestmark = [pytest.mark.browser, pytest.mark.django_db(transaction=True)]


@pytest.mark.parametrize("role", BROWSER_ROLES)
def test_no_console_errors(role, live_server, pw_browser):
    """Every allowed page for this role should render with zero console errors."""
    test_data = make_test_data()
    user = role_user(role)
    context = create_session_context(pw_browser, live_server, user)

    failures = []
    try:
        for page_def in PAGES:
            if role not in page_def.allowed_roles:
                continue

            url = live_server.url + resolve_url(page_def, test_data)
            page, status_code, errors = visit_and_capture_errors(context, url)
            page.close()

            if page_def.expect_redirect:
                if errors:
                    failures.append(f"{page_def.url_name}: console errors:\n" + "\n".join(errors))
            else:
                if status_code != 200:
                    failures.append(f"{page_def.url_name}: expected 200, got {status_code}")
                if errors:
                    failures.append(f"{page_def.url_name}: console errors:\n" + "\n".join(errors))
    finally:
        context.close()

    assert not failures, f"Failures for {role}:\n" + "\n".join(failures)
