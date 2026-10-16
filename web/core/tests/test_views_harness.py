"""The view property harness's own checks: its oracle refuses what it should, and its tables stay in step with the
code they stand for."""

import pytest
from django.http import HttpResponse

from core.permission_constants import PERMISSION_MAP
from core.tests.roles import ROLE_GROUPS
from core.tests.views_harness import (
    Answer,
    Drawn,
    ViewCase,
    build,
    check_answer,
    may,
    refuses_visitor,
    unknown_refusal,
)


def _answer(status: int) -> Answer:
    return Answer(HttpResponse(status=status), b"", [], [])


def test_a_400_to_a_refused_role_passes_only_for_a_nul() -> None:
    """A view that validates its input before its permission check would answer a refused role 400."""
    case = ViewCase("ticket_list", frozenset({"admin"}))
    plain = Drawn("red_team", "GET", "/tickets/", {"search": ["x"]}, {}, None, {}, None)
    with pytest.raises(AssertionError, match="a role the page refuses got 400"):
        check_answer(case, plain, _answer(400))
    with_nul = Drawn("red_team", "GET", "/tickets/", {"search": ["\x00"]}, {}, None, {}, None)
    check_answer(case, with_nul, _answer(400))


def test_every_permission_group_has_a_role() -> None:
    """A group added to PERMISSION_MAP with no role would leave may() empty for the pages it gates, and "everyone
    refused" would pass."""
    held = {group for groups in ROLE_GROUPS.values() if groups for group in groups}
    missing = sorted({group for groups in PERMISSION_MAP.values() for group in groups} - held)
    assert not missing, (
        f"Authentik groups in PERMISSION_MAP no test role holds (add them to core.tests.roles): {missing}"
    )
    assert all(may(permission) for permission in PERMISSION_MAP), "a permission no role holds"


def test_a_refusal_that_mails_or_calls_out_fails() -> None:
    case = ViewCase("ticket_list", frozenset({"admin"}))
    drawn = Drawn("red_team", "GET", "/tickets/", {}, {}, None, {}, None)
    with pytest.raises(AssertionError, match="sent 1 email"):
        check_answer(case, drawn, Answer(HttpResponse(status=403), b"", [], [], mailed=1))
    with pytest.raises(AssertionError, match="called another service 1 time"):
        check_answer(case, drawn, Answer(HttpResponse(status=403), b"", [], [], outbound=1))


def test_every_skipped_browser_role_is_one_the_page_lets_in() -> None:
    """A page's skip_roles entry for a role its row refuses is stale (the role was never going to be visited)."""
    from browser_tests.page_registry import PAGES

    from core.tests.roles import BROWSER_ROLES
    from core.tests.view_rows import allowed_roles

    stale = [
        f"{page.url_name}: {role}"
        for page in PAGES
        for role in page.skip_roles
        if role not in BROWSER_ROLES or role not in allowed_roles(page.url_name)
    ]
    assert not stale, f"skip_roles entries for roles the page's row refuses: {stale}"


def test_a_stream_whose_operation_raised_fails() -> None:
    """run_detached turns an operation's exception into a "Failed: ..." line with a 200, inline or threaded."""
    from django.http import StreamingHttpResponse

    from core.utils import ndjson_done

    case = ViewCase("admin_competition_action", frozenset({"admin"}))
    drawn = Drawn("admin", "POST", "/ops/admin/competition/action/", {}, {}, None, {}, None)
    line = ndjson_done("Failed: boom", success=False)
    response = StreamingHttpResponse(iter([line]), content_type="application/x-ndjson")
    with pytest.raises(AssertionError, match="the streamed operation raised"):
        check_answer(case, drawn, Answer(response, line.encode(), [], []))


def test_a_nul_counts_only_where_the_refusals_look() -> None:
    from django.core.files.uploadedfile import SimpleUploadedFile

    case = ViewCase("ticket_list", frozenset({"admin"}))
    in_upload_name = {"attachment": SimpleUploadedFile("a\x00.png", b"x")}
    assert not build(case, role="red_team", method="POST", name="ticket_list", kwargs={}, query={},
                     fields=in_upload_name).carries_nul()  # fmt: skip
    assert not build(case, role="red_team", method="GET", name="ticket_list", kwargs={}, query={},
                     fields={"search": "\x00"}).carries_nul()  # fmt: skip
    assert build(case, role="red_team", method="POST", name="ticket_list", kwargs={}, query={},
                 fields={"search": "\x00"}).carries_nul()  # fmt: skip


def test_the_blue_team_role_user_is_a_team_member(db: object) -> None:
    """role_user("blue_team") makes Team 1 when it doesn't exist: without it, the user holds a group and no team."""
    from core.auth_utils import get_user_team
    from core.tests.roles import role_user

    team = get_user_team(role_user("blue_team"))
    assert team is not None and team.team_number == 1


def _redirect(errors: list[str], warnings: list[str] | None = None) -> Answer:
    response = HttpResponse(status=302)
    response["Location"] = "/elsewhere/"
    return Answer(response, b"", [], errors, warnings or [])


def _page(status: int, text: str) -> Answer:
    return Answer(HttpResponse(text, status=status), text.encode(), [], [])


@pytest.mark.parametrize(
    "answer",
    [
        _redirect(["You can only delete your own incident reports"]),  # a POST's error redirect
        _redirect([], ["You don't have permission to do that"]),  # a warning redirect
        _page(200, "<h1>Access Denied</h1>"),  # a page saying no
        _page(400, '{"error": "Not allowed"}'),
    ],
)
def test_an_answer_in_refusal_words_isnt_taken_for_getting_in(answer: Answer) -> None:
    """every_allowed_role_gets_in counts these as got in by their shape (no 403, no redirect home, a POST), so
    their words must keep the visitor out, or a row too wide for its view passes."""
    assert not refuses_visitor(answer, "POST")
    assert unknown_refusal(answer)


def test_an_answer_letting_the_visitor_in_has_no_refusal_words() -> None:
    assert unknown_refusal(_page(200, "<h1>Tickets</h1>")) is None
    assert unknown_refusal(_redirect([], ["Nothing changed"])) is None


def test_a_gets_error_redirect_refuses_the_visitor_and_a_posts_doesnt() -> None:
    assert refuses_visitor(_redirect(["Nothing to send"]), "GET")
    assert not refuses_visitor(_redirect(["Nothing to send"]), "POST")
