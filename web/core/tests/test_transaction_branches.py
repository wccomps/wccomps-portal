"""Code that branches on whether it runs inside a transaction (connection.in_atomic_block) must have a test of the
branch production takes, outside one: a test runs inside pytest-django's transaction unless it is marked
django_db(transaction=True), so an ordinary test only ever takes the other branch.

Each such function is listed with the test module that runs it outside a transaction; a new one fails until it is
listed, and a listed module must carry that marker.
"""

import ast

from core.tests.guards import REPO_DIR, fail_with
from core.tests.test_no_unused_code import python_trees
from core.tests.test_property_coverage import _functions

# "path:function" that reads in_atomic_block -> the test module that runs it outside a transaction
OUTSIDE_TRANSACTION_TESTS = {
    "web/registration/services.py:close_out_event": "web/core/tests/test_close_out_action.py",
    "web/core/utils.py:_in_transaction": "web/core/tests/test_competition_start_stop_web.py",
}


def transaction_branches() -> dict[str, str]:
    found = {}
    for path, tree in python_trees().items():
        if "/tests/" in path or not path.startswith(("web/", "bot/")):
            continue
        for name, function in _functions(tree):
            if any(isinstance(n, ast.Attribute) and n.attr == "in_atomic_block" for n in ast.walk(function)):
                found[f"{path}:{name}"] = "reads connection.in_atomic_block"
    return found


def test_every_transaction_branch_is_tested_outside_a_transaction() -> None:
    found = transaction_branches()
    fail_with("Functions that branch on in_atomic_block with no test outside a transaction", found,
              dict.fromkeys(OUTSIDE_TRANSACTION_TESTS, "listed"))  # fmt: skip
    unmarked = [
        module
        for module in OUTSIDE_TRANSACTION_TESTS.values()
        if "django_db(transaction=True)" not in (REPO_DIR / module).read_text(encoding="utf-8")
    ]
    assert not unmarked, f"Listed modules with no django_db(transaction=True) test: {unmarked}"
