"""The view property harness's own checks: its oracle refuses what it should, and its tables stay in step with the
code they stand for."""

import sys

from browser_tests.page_registry import PAGES
from django.http import HttpResponse

from core.permission_constants import PERMISSION_MAP
from core.tests.test_property_coverage import VIEW_MODULES
from core.tests.views_harness import (
    ROLES,
    Answer,
    Drawn,
    ViewCase,
    check_answer,
    may,
    offline,
    run_detached_callers,
)


def _answer(status: int) -> Answer:
    return Answer(HttpResponse(status=status), b"", [], [])


def test_a_400_to_a_refused_role_passes_only_for_a_nul() -> None:
    """A view that validates its input before its permission check would answer a refused role 400."""
    case = ViewCase("ticket_list", frozenset({"admin"}))
    plain = Drawn("red_team", "GET", "/tickets/", {"search": ["x"]}, {}, None, {}, None)
    try:
        check_answer(case, plain, _answer(400))
    except AssertionError:
        pass
    else:
        raise AssertionError("a 400 without a NUL passed as a refusal")
    with_nul = Drawn("red_team", "GET", "/tickets/", {"search": ["\x00"]}, {}, None, {}, None)
    check_answer(case, with_nul, _answer(400))


def test_every_permission_group_has_a_role() -> None:
    """A group added to PERMISSION_MAP with no role would leave may() empty for the pages it gates, and "everyone
    refused" would pass."""
    held = {group for groups in ROLES.values() if groups for group in groups}
    missing = sorted({group for groups in PERMISSION_MAP.values() for group in groups} - held)
    assert not missing, (
        f"Authentik groups in PERMISSION_MAP no test role holds (add them to core.tests.roles): {missing}"
    )
    assert all(may(permission) for permission in PERMISSION_MAP), "a permission no role holds"


def test_the_browser_tests_visit_each_page_as_a_role_it_lets_in() -> None:
    allowed = {case.name: case.allowed for module in VIEW_MODULES for case in module.CASES}
    wrong = [
        f"{page.url_name}: {role}"
        for page in PAGES
        for role in page.allowed_roles
        if role not in allowed[page.url_name]
    ]
    assert not wrong, f"page_registry visits pages as roles they refuse: {wrong}"


def test_offline_patches_every_run_detached_caller() -> None:
    """A streaming view whose run_detached isn't patched runs in a thread that sees none of the test's rows."""
    from core import utils

    callers = {module.__name__ for module in run_detached_callers()}
    assert {"core.admin_views.competition", "packets.views", "scoring.views.export"} <= callers
    with offline():
        unpatched = [
            name
            for name, module in list(sys.modules.items())
            if getattr(module, "run_detached", None) is utils.run_detached and module is not utils
        ]
    assert not unpatched, f"offline() left run_detached unpatched in {unpatched}"


def test_a_refusal_that_mails_or_calls_out_fails() -> None:
    case = ViewCase("ticket_list", frozenset({"admin"}))
    drawn = Drawn("red_team", "GET", "/tickets/", {}, {}, None, {}, None)
    for answer in (
        Answer(HttpResponse(status=403), b"", [], [], mailed=1),
        Answer(HttpResponse(status=403), b"", [], [], outbound=1),
    ):
        try:
            check_answer(case, drawn, answer)
        except AssertionError:
            continue
        raise AssertionError(f"a refusal that sent mail or called out passed: {answer}")
