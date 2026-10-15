"""The ticket page's 30-second poll leaves its aria-live region alone until something changes, then swaps it."""

import pytest

from .conftest import _create_role_user, create_session_context, make_test_data

pytestmark = [pytest.mark.browser, pytest.mark.django_db(transaction=True)]

COMMENTS = "[hx-select='#comments-list']"
# Counts content changes inside the comments region from now on (what a live region announces; htmx's
# htmx-request class on the wrapper during a request is not content)
WATCH = """region => {
    window.regionMutations = 0;
    new MutationObserver(records => { window.regionMutations += records.length; })
        .observe(region, {childList: true, subtree: true, characterData: true});
}"""


def test_the_comment_poll_swaps_only_when_a_comment_arrives(live_server, pw_browser):
    from ticketing.models import TicketComment

    viewer = _create_role_user("ticketing_support", None)
    ticket = make_test_data()["ticket"]
    context = create_session_context(pw_browser, live_server, viewer)
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda err: errors.append(str(err)))
    try:
        page.clock.install()
        page.goto(f"{live_server.url}/tickets/{ticket.ticket_number}/", wait_until="networkidle")
        page.get_by_role("button", name="Comments").click()  # collapsed while the ticket has none
        region = page.locator(COMMENTS)
        assert region.get_attribute("aria-live") == "polite"
        region.evaluate(WATCH)

        def poll():
            with page.expect_response(lambda r: "/dynamic/?comments_version=" in r.url) as response:
                page.clock.run_for("00:31")
            return response.value

        quiet = poll()
        assert quiet.status == 204
        page.wait_for_timeout(200)
        assert page.evaluate("window.regionMutations") == 0

        TicketComment.objects.create(ticket=ticket, author=viewer, comment_text="Box is back up")
        changed = poll()
        assert changed.status == 200
        region.get_by_text("Box is back up").wait_for()
        assert page.evaluate("window.regionMutations") > 0

        assert poll().status == 204
        assert not errors, errors
    finally:
        context.close()
