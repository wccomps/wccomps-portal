"""Every entry point for user-tainted input has a property test, found from the code rather than a hand-kept list.

The entry points: every Django form class in the project (imported, so a form in an admin or view module counts);
every function that parses CSV or an uploaded file (found by what it does: calls a csv reader, takes an
UploadedFile, reads request.FILES, or reads an upload, file or attachment); every URL pattern and admin model page;
and the bot's slash commands, modals, buttons, selects and message listeners.

Each must be registered against property tests (FORM_CASES, PARSERS, a row in a VIEW_MODULES module's CASES for a
URL pattern or admin model, or a row in the bot's CASES) or listed in EXCEPTIONS with the reason. A
registry entry or exception that matches no entry point fails too, so neither can go stale. Fix a failure by adding
a property test case for the new entry point (the test module's docstring says how), or an exception with a reason.
"""

import ast
import importlib
import pkgutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from django import forms
from django.apps import apps
from django.contrib import admin
from django.urls import URLPattern, URLResolver, get_resolver

from bot.tests import test_properties_bot
from core.tests import test_properties_forms, test_properties_parsers
from core.tests.test_no_unused_code import REPO_DIR, Unused, fail_with, python_trees
from core.tests.view_rows import VIEW_MODULES

WEB_DIR = REPO_DIR / "web"

# ---------------------------------------------------------------------------------------------------------------
# Finding the entry points
# ---------------------------------------------------------------------------------------------------------------


def project_apps() -> list[str]:
    return [config.name for config in apps.get_app_configs() if Path(config.path).is_relative_to(WEB_DIR)]


def import_project_modules() -> None:
    """Import every module of the project's apps (not tests or migrations), so every form class is defined."""
    for app in project_apps():
        package = importlib.import_module(app)
        for module in pkgutil.walk_packages(package.__path__, prefix=f"{app}."):
            parts = set(module.name.split("."))
            if parts & {"tests", "migrations"} or module.name.rsplit(".", 1)[-1].startswith("test_"):
                continue
            importlib.import_module(module.name)


def form_entry_points() -> dict[str, str]:
    import_project_modules()
    prefixes = tuple(f"{app}." for app in project_apps())
    found: dict[str, str] = {}
    stack: list[type[forms.BaseForm]] = [forms.BaseForm]
    while stack:
        cls = stack.pop()
        stack.extend(cls.__subclasses__())
        if cls.__module__.startswith(prefixes) and ".tests" not in cls.__module__:
            found[f"{cls.__module__}.{cls.__qualname__}"] = cls.__module__
    return found


CSV_READERS = {"csv.reader", "csv.DictReader", "csv.Sniffer"}
UPLOAD_NAMES = ("file", "upload", "attachment")


def import_aliases(tree: ast.AST) -> dict[str, str]:
    """What each name a module imports refers to: {"c": "csv"} for import csv as c, {"R": "csv.DictReader"} for
    from csv import DictReader as R."""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                aliases[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return aliases


def _resolved(func: ast.expr, aliases: dict[str, str]) -> str:
    """The dotted name a call's function refers to, through the module's imports."""
    called = ast.unparse(func)
    head, _, rest = called.partition(".")
    return f"{aliases[head]}.{rest}".rstrip(".") if head in aliases else called


def _parser_reasons(function: ast.FunctionDef | ast.AsyncFunctionDef, aliases: dict[str, str]) -> set[str]:
    reasons: set[str] = set()
    for node in ast.walk(function):
        if isinstance(node, ast.Call):
            called = _resolved(node.func, aliases)
            if called in CSV_READERS:
                reasons.add(called)
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr in {"read", "chunks", "readlines"}
                and any(n in ast.unparse(node.func.value).lower() for n in UPLOAD_NAMES)
            ):
                reasons.add(f"{ast.unparse(node.func)}()")
        elif isinstance(node, ast.Attribute) and node.attr == "FILES":
            reasons.add("request.FILES")
        elif (
            isinstance(node, ast.arg) and node.annotation is not None and "UploadedFile" in ast.unparse(node.annotation)
        ):
            reasons.add("an UploadedFile parameter")
    return reasons


def _functions(tree: ast.AST, prefix: str = "") -> Iterator[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef):
            yield from _functions(node, f"{prefix}{node.name}.")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield f"{prefix}{node.name}", node
            yield from _functions(node, f"{prefix}{node.name}.")


def parser_entry_points() -> dict[str, str]:
    found: dict[str, str] = {}
    for path, tree in python_trees().items():
        aliases = import_aliases(tree)
        for name, function in _functions(tree):
            if reasons := _parser_reasons(function, aliases):
                found[f"{path}:{name}"] = ", ".join(sorted(reasons))
    return found


def _patterns(resolver: URLResolver, prefix: str = "") -> Iterator[str]:
    for entry in resolver.url_patterns:
        route = prefix + str(entry.pattern)
        if isinstance(entry, URLResolver):
            if entry.app_name == "admin":
                yield route  # the admin site, whose model pages are listed one by one below
            else:
                yield from _patterns(entry, route)
        elif isinstance(entry, URLPattern):
            yield route


def url_entry_points() -> dict[str, str]:
    found = {f"url:{route}": "a URL pattern" for route in _patterns(get_resolver())}
    for model in admin.site._registry:
        found[f"admin:{model._meta.label}"] = "a Django admin model page"
    return found


def _decorators(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    return [ast.unparse(d.func if isinstance(d, ast.Call) else d) for d in node.decorator_list]


def bot_entry_points() -> dict[str, str]:
    found: dict[str, str] = {}
    for path, tree in python_trees().items():
        if not path.startswith("bot/"):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                bases = {ast.unparse(b).split("[")[0] for b in node.bases}
                for kind in ("Modal", "Button", "Select", "DynamicItem"):
                    if bases & {f"discord.ui.{kind}", f"ui.{kind}"}:
                        found[f"bot:{kind.lower()}:{path}:{node.name}"] = f"a discord.ui.{kind}"
        for name, function in _functions(tree):
            decorators = _decorators(function)
            if any(d.endswith((".command", "context_menu")) for d in decorators):
                found[f"bot:command:{path}:{name}"] = "a slash command"
            if any(d in {"discord.ui.button", "discord.ui.select", "ui.button", "ui.select"} for d in decorators):
                found[f"bot:button:{path}:{name}"] = "a view's button or select callback"
            if function.name.startswith("on_message") and (
                "commands.Cog.listener" in decorators or any(d.endswith(".event") for d in decorators)
            ):
                found[f"bot:listener:{path}:{name}"] = "a message listener"
    return found


# ---------------------------------------------------------------------------------------------------------------
# What covers them
# ---------------------------------------------------------------------------------------------------------------

# Each runs its properties for every case in FORM_CASES (parametrized over CASES), so a case covers its form
FORM_TESTS = (
    test_properties_forms.test_any_post_validates_without_raising,
    test_properties_forms.test_valid_post_fits_its_fields_is_stored_and_cleans_the_same,
)

EXCEPTIONS: dict[str, str] = {}


@pytest.mark.django_db
def test_every_form_has_property_tests() -> None:
    found = form_entry_points()
    fail_with(
        "Forms without a property test case (add one to FORM_CASES in test_properties_forms.py)",
        [Unused(key, module) for key, module in found.items() if key not in test_properties_forms.FORM_CASES],
        {},
    )
    stale = sorted(set(test_properties_forms.FORM_CASES) - set(found))
    assert not stale, f"FORM_CASES has cases for forms that no longer exist: {stale}"
    for test in FORM_TESTS:
        cases = [m.args[1] for m in getattr(test, "pytestmark", []) if m.name == "parametrize"]
        assert cases == [test_properties_forms.CASES], f"{test.__name__} must run every case in FORM_CASES"


def test_every_upload_and_csv_parser_has_property_tests() -> None:
    found = parser_entry_points()
    # The bot's listeners that read an attachment are driven by their rows in the bot's CASES
    bot_listeners = {case.function for case in test_properties_bot.CASES if hasattr(case, "function")}
    registry = {**test_properties_parsers.PARSERS, **dict.fromkeys(bot_listeners & set(found), ())}
    uncovered = [Unused(key, reasons) for key, reasons in found.items() if key not in registry]
    parser_exceptions = {k: v for k, v in EXCEPTIONS.items() if not k.startswith(("url:", "admin:", "bot:"))}
    fail_with("Parsers of uploads or CSV without a property test (add them to PARSERS)", uncovered, parser_exceptions)
    stale = sorted(set(registry) - set(found))
    assert not stale, f"PARSERS lists functions that no longer parse uploads or CSV: {stale}"
    not_properties = [
        f"{key}: {test.__name__}"
        for key, tests in test_properties_parsers.PARSERS.items()
        for test in tests
        if not getattr(test, "is_hypothesis_test", False)
    ]
    assert not not_properties, f"PARSERS lists tests that aren't Hypothesis property tests: {not_properties}"


def view_cases() -> dict[str, str]:
    """Each URL pattern or admin model a view property test row covers, with the module it is in."""
    return {case.entry_point: module.__name__ for module in VIEW_MODULES for case in module.CASES}


def test_every_url_pattern_has_property_tests() -> None:
    found = url_entry_points()
    covered = view_cases()
    fail_with(
        "URL patterns and admin pages without a property test row (add one to a test_properties_views_* module)",
        [Unused(key, where) for key, where in found.items() if key not in covered],
        {k: v for k, v in EXCEPTIONS.items() if k.startswith(("url:", "admin:"))},
    )
    stale = sorted(set(covered) - set(found))
    assert not stale, f"View property rows for URL patterns that no longer exist: {stale}"
    for module in VIEW_MODULES:
        tests = (
            module.test_any_request_is_answered_safely,
            module.test_every_field_is_escaped_where_echoed,
            module.test_every_allowed_role_gets_in,
        )
        for test in tests:
            cases = [m.args[1] for m in getattr(test, "pytestmark", []) if m.name == "parametrize"]
            assert cases == [module.CASES], f"{module.__name__}.{test.__name__} must run every row of CASES"


def test_every_bot_entry_point_has_property_tests() -> None:
    found = bot_entry_points()
    covered = {case.entry_point for case in test_properties_bot.CASES}
    fail_with(
        "Bot commands, modals, buttons and listeners without property tests (add a row to the bot's CASES)",
        [Unused(key, where) for key, where in found.items() if key not in covered],
        {k: v for k, v in EXCEPTIONS.items() if k.startswith("bot:")},
    )
    stale = sorted(covered - set(found))
    assert not stale, f"Bot property rows for entry points that no longer exist: {stale}"
    test = test_properties_bot.test_any_input_is_answered_within_discords_limits
    cases = [m.args[1] for m in getattr(test, "pytestmark", []) if m.name == "parametrize"]
    assert cases == [test_properties_bot.CASES], f"{test.__name__} must run every row of CASES"


CSV_EXPORT_MODULE = "web/core/csv_export.py"
RAW_CSV_WRITERS = {"csv.writer", "csv.DictWriter"}
# Writers that may stay plain, each with why its cells can't carry a formula
CSV_WRITER_EXCEPTIONS = {
    "web/core/authentik_utils.py:credentials_csv": "generated usernames and passwords, not user text; a password "
    "starting with @ or + must read back exactly, without the formula guard's quote",
}


def _nodes_in_scope(node: ast.AST, scope: str = "<module>", prefix: str = "") -> Iterator[tuple[str, ast.AST]]:
    """Every node under node, with the qualified name of the function (or class) it sits in."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            name = f"{prefix}{child.name}"
            yield from _nodes_in_scope(child, name, f"{name}.")
        else:
            yield scope, child
            yield from _nodes_in_scope(child, scope, prefix)


def csv_writer_calls(tree: ast.AST) -> dict[str, str]:
    """csv.writer/DictWriter calls in a module (however csv was imported), keyed by the function they are in."""
    aliases = import_aliases(tree)
    return {
        scope: _resolved(node.func, aliases)
        for scope, node in _nodes_in_scope(tree)
        if isinstance(node, ast.Call) and _resolved(node.func, aliases) in RAW_CSV_WRITERS
    }


def raw_csv_writers() -> dict[str, str]:
    """csv.writer/DictWriter calls outside core.csv_export, keyed by path and function."""
    return {
        f"{path}:{scope}": call
        for path, tree in python_trees().items()
        if path != CSV_EXPORT_MODULE
        for scope, call in csv_writer_calls(tree).items()
    }


def test_every_csv_writer_neutralises_formulas() -> None:
    """User text in a CSV must not run as a spreadsheet formula: write CSV with core.csv_export.csv_writer."""
    fail_with(
        "CSV writers that don't go through core.csv_export.csv_writer",
        [Unused(key, call) for key, call in raw_csv_writers().items()],
        CSV_WRITER_EXCEPTIONS,
    )


@pytest.mark.parametrize(
    ("source", "parser", "writer"),
    [
        ("import csv\ndef f(t, o):\n    csv.DictReader(t)\n    csv.writer(o)\n", True, True),
        ("from csv import DictReader\ndef f(t):\n    return DictReader(t)\n", True, False),
        ("import csv as c\ndef f(t, o):\n    c.reader(t)\n    c.DictWriter(o, [])\n", True, True),
        ("from csv import writer as w\ndef f(o):\n    w(o)\n", False, True),
        ("def f(reader):\n    reader(1)\n", False, False),
    ],
)
def test_the_guards_find_csv_readers_and_writers_however_they_are_imported(
    source: str, parser: bool, writer: bool
) -> None:
    tree = ast.parse(source)
    assert bool(_parser_reasons(tree.body[-1], import_aliases(tree))) is parser
    assert bool(csv_writer_calls(tree)) is writer


def test_no_property_test_pins_its_example_count() -> None:
    """The Hypothesis profile (ci or deep, or its -views variant for the view tests; conftest.py) sets how many
    examples a test runs; a test that pins max_examples would run the same few under HYPOTHESIS_PROFILE=deep."""
    tracked = subprocess.run(
        ["git", "ls-files", "-z", "*.py"],  # noqa: S607
        cwd=REPO_DIR,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\0")
    pinned = [
        f"{path}:{node.lineno}"
        for path in tracked
        if path and path != "conftest.py" and (REPO_DIR / path).is_file()
        for node in ast.walk(ast.parse((REPO_DIR / path).read_text(encoding="utf-8")))
        if isinstance(node, ast.Call) and any(k.arg == "max_examples" for k in node.keywords)
    ]
    assert not pinned, f"Tests that pin max_examples: {pinned}"
