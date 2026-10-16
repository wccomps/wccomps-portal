"""Test role-specific UI elements using Playwright."""

import pytest

from core.tests.roles import role_user

from .conftest import (
    create_session_context,
    make_test_data,
    resolve_url,
    visit_and_capture_errors,
)
from .page_registry import get_element_check_cases

pytestmark = [pytest.mark.browser, pytest.mark.django_db(transaction=True)]


# -- Element presence/absence tests --

_element_cases = get_element_check_cases()
_element_ids = [f"{p.url_name}--{r}" for p, r in _element_cases]


@pytest.mark.parametrize("page_def,role", _element_cases, ids=_element_ids)
def test_role_specific_elements(page_def, role, live_server, pw_browser):
    """Verify role-specific UI elements are present or absent."""
    test_data = make_test_data()
    user = role_user(role)
    context = create_session_context(pw_browser, live_server, user)

    try:
        url = live_server.url + resolve_url(page_def, test_data)
        page, status_code, _errors = visit_and_capture_errors(context, url)

        assert status_code == 200, f"{page_def.url_name} as {role}: expected 200, got {status_code}"

        checks = page_def.checks[role]

        for selector in checks.get("present", []):
            count = page.locator(selector).count()
            assert count > 0, f"{page_def.url_name} as {role}: expected '{selector}' to be present, found 0"

        for selector in checks.get("absent", []):
            count = page.locator(selector).count()
            assert count == 0, f"{page_def.url_name} as {role}: expected '{selector}' to be absent, found {count}"

    finally:
        context.close()
