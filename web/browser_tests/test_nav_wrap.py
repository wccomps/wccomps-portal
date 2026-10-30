"""The nav bar wraps its links onto more rows rather than running off the page, and keeps its tools together."""

import pytest

from core.tests.roles import role_user

from .conftest import create_session_context

pytestmark = [pytest.mark.browser, pytest.mark.django_db(transaction=True)]

LAYOUT = """() => {
    const nav = document.querySelector('.primary-navigation');
    const right = nav.getBoundingClientRect().right;
    const items = Array.from(nav.querySelectorAll('.nav-links a, .nav-utils > *'));
    const tools = Array.from(nav.querySelectorAll('.nav-utils > *'))
        .map(el => el.getBoundingClientRect())
        .filter(r => r.width)
        .map(r => Math.round((r.top + r.bottom) / 2));
    return {
        outside: items.filter(el => el.getBoundingClientRect().right > right + 1).map(el => el.textContent.trim()),
        broken: Array.from(nav.querySelectorAll('.nav-links a'))
            .filter(a => a.getClientRects().length > 1 || a.getBoundingClientRect().height > 40)
            .map(a => a.textContent.trim()),
        toolRows: new Set(tools).size,
        scrolls: document.documentElement.scrollWidth > innerWidth,
    };
}"""


@pytest.mark.parametrize("width", [1280, 800])
def test_an_admins_nav_fits_its_row_at_any_desktop_width(live_server, pw_browser, width):
    """An admin sees every section link, the most any role does."""
    context = create_session_context(pw_browser, live_server, role_user("admin"))
    page = context.new_page()
    try:
        page.set_viewport_size({"width": width, "height": 700})
        page.goto(f"{live_server.url}/ops/school-info/")

        layout = page.evaluate(LAYOUT)
        assert layout["outside"] == []
        assert layout["broken"] == []
        assert layout["toolRows"] == 1
        assert not layout["scrolls"]
    finally:
        context.close()
