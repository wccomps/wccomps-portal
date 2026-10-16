"""The view property harness's own checks: its oracle refuses what it should, and its tables stay in step with the
code they stand for."""

import ast
import importlib

import pytest
from browser_tests.page_registry import PAGES
from django.http import HttpResponse

from core.permission_constants import PERMISSION_MAP
from core.tests.roles import ROLE_GROUPS
from core.tests.test_no_unused_code import python_trees
from core.tests.test_property_coverage import VIEW_MODULES
from core.tests.views_harness import (
    RUN_DETACHED,
    Answer,
    Drawn,
    ViewCase,
    check_answer,
    may,
    offline,
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


def test_the_browser_tests_visit_each_page_as_a_role_it_lets_in() -> None:
    allowed = {case.name: case.allowed for module in VIEW_MODULES for case in module.CASES}
    wrong = [
        f"{page.url_name}: {role}"
        for page in PAGES
        for role in page.allowed_roles
        if role not in allowed[page.url_name]
    ]
    assert not wrong, f"page_registry visits pages as roles they refuse: {wrong}"


def _run_detached_users() -> dict[str, set[str]]:
    """From the source, not from what is loaded: each project module that names run_detached (an import of it, under
    any name, or an attribute call), with the names it binds it to."""
    users: dict[str, set[str]] = {}
    for path, tree in python_trees().items():
        if not path.startswith("web/") or "/tests/" in path or path == "web/core/utils.py":
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in ("core.utils", "utils"):
                names = {a.asname or a.name for a in node.names if a.name == "run_detached"}
            elif isinstance(node, ast.Attribute) and node.attr == "run_detached":
                names = {""}
            else:
                continue
            module = path.removeprefix("web/").removesuffix(".py").replace("/", ".")
            users.setdefault(module, set()).update(names)
    return users


def test_offline_patches_every_run_detached_caller() -> None:
    """A streaming view whose run_detached isn't patched runs in a thread that sees none of the test's rows."""
    from core import utils

    users = _run_detached_users()
    assert users, "no module uses run_detached: the scan is broken"
    with offline():
        assert utils.run_detached is not RUN_DETACHED, "offline() left core.utils.run_detached unpatched"
        unpatched = [
            f"{module}.{name}"
            for module, names in users.items()
            for name in names - {""}
            if getattr(importlib.import_module(module), name) is RUN_DETACHED
        ]
    assert not unpatched, f"offline() left run_detached unpatched in {unpatched}"


def test_a_refusal_that_mails_or_calls_out_fails() -> None:
    case = ViewCase("ticket_list", frozenset({"admin"}))
    drawn = Drawn("red_team", "GET", "/tickets/", {}, {}, None, {}, None)
    with pytest.raises(AssertionError, match="sent 1 email"):
        check_answer(case, drawn, Answer(HttpResponse(status=403), b"", [], [], mailed=1))
    with pytest.raises(AssertionError, match="called another service 1 time"):
        check_answer(case, drawn, Answer(HttpResponse(status=403), b"", [], [], outbound=1))
