"""The nav bar wraps its links onto more rows rather than running off the page, and keeps its tools together, beside
the first row of links when there is room."""

import pytest

from core.tests.roles import role_user

from .conftest import create_session_context

pytestmark = [pytest.mark.browser, pytest.mark.django_db(transaction=True)]

LAYOUT = """() => {
    const nav = document.querySelector('.primary-navigation');
    const links = Array.from(nav.querySelectorAll('.nav-links a'));
    const oneLine = Math.min(...links.map(a => a.getBoundingClientRect().height));
    const middle = r => (r.top + r.bottom) / 2;
    const firstRow = Math.min(...links.map(a => middle(a.getBoundingClientRect())));
    const tools = Array.from(nav.querySelectorAll('.nav-utils > *'))
        .map(el => el.getBoundingClientRect())
        .filter(r => r.width);
    const name = document.createRange();
    name.selectNodeContents(document.getElementById('site-name'));
    const nameLines = new Set(Array.from(name.getClientRects(), r => Math.round(r.top)));
    return {
        offPage: [...links, ...nav.querySelectorAll('.nav-utils > *')]
            .filter(el => el.getBoundingClientRect().right > document.documentElement.clientWidth + 1)
            .map(el => el.textContent.trim()),
        broken: links.filter(a => a.getBoundingClientRect().height > 1.5 * oneLine).map(a => a.textContent.trim()),
        tools: tools.length,
        toolRows: new Set(tools.map(r => Math.round(middle(r)))).size,
        toolsBesideFirstRow: tools.every(r => Math.abs(middle(r) - firstRow) < oneLine / 2),
        nameLines: nameLines.size,
    };
}"""


@pytest.mark.parametrize(("width", "beside"), [(1600, True), (1280, True), (1100, True), (800, False)])
def test_an_admins_nav_stays_on_the_page_at_desktop_widths(live_server, pw_browser, width, beside):
    """An admin sees every section link, the most any role does. From 1100 up the nav sits beside the name and the
    tools beside the first row of links: another row holds more links, never the tools alone. At 800 the links keep
    their width and the tools go below them."""
    context = create_session_context(pw_browser, live_server, role_user("admin"))
    page = context.new_page()
    try:
        page.set_viewport_size({"width": width, "height": 700})
        page.goto(f"{live_server.url}/ops/school-info/")

        layout = page.evaluate(LAYOUT)
        assert layout["offPage"] == []
        assert layout["broken"] == []
        assert layout["tools"] == 4
        assert layout["toolRows"] == 1
        assert layout["nameLines"] == 1
        assert layout["toolsBesideFirstRow"] == beside
    finally:
        context.close()


def test_on_a_phone_a_long_name_leaves_log_out_whole_and_on_the_screen(live_server, pw_browser):
    context = create_session_context(pw_browser, live_server, role_user("admin", username="x" * 48))
    page = context.new_page()
    try:
        page.set_viewport_size({"width": 375, "height": 800})
        page.goto(f"{live_server.url}/ops/school-info/")
        page.locator(".nav-toggle").click()

        log_out = page.get_by_role("button", name="Log out")
        box = log_out.bounding_box()
        assert box
        assert box["x"] + box["width"] <= 375
        lines = log_out.evaluate(
            "el => { const r = document.createRange(); r.selectNodeContents(el);"
            " return new Set(Array.from(r.getClientRects(), rect => Math.round(rect.top))).size; }"
        )
        assert lines == 1
        assert page.evaluate("document.documentElement.scrollWidth") <= 375
    finally:
        context.close()
