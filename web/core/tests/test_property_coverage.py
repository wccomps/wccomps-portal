"""Every entry point for user-tainted input has a property test, found from the code rather than a hand-kept list.

The entry points: every Django form class in the project (imported, so a form in an admin or view module counts);
every function that parses CSV or an uploaded file (found by what it does: calls a csv reader, takes an
UploadedFile, reads request.FILES, or reads an upload, file or attachment); every URL pattern and admin model page;
and the bot's slash commands, modals, buttons, selects and message listeners.

Each must be registered against property tests (FORM_CASES, PARSERS) or listed in EXCEPTIONS with the reason. A
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

from core.tests import (
    test_properties_forms,
    test_properties_parsers,
    test_properties_views_admin_site,
    test_properties_views_core,
    test_properties_views_orange,
    test_properties_views_packets,
    test_properties_views_registration,
    test_properties_views_scoring,
    test_properties_views_ticketing,
)
from core.tests.test_no_unused_code import REPO_DIR, Unused, fail_with, python_trees

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

PR_C = "covered in PR C (the Discord bot)"


BOT_FOR_PR_C = [
    "bot:button:bot/cogs/help_panels.py:LinkButton",
    "bot:button:bot/cogs/help_panels.py:TicketButton",
    "bot:button:bot/ticket_dashboard.py:TicketActionView.cancel_button",
    "bot:button:bot/ticket_dashboard.py:TicketActionView.claim_button",
    "bot:button:bot/ticket_dashboard.py:TicketActionView.resolve_button",
    "bot:button:bot/unified_dashboard.py:DashboardControlView.filter_all",
    "bot:button:bot/unified_dashboard.py:DashboardControlView.filter_claimed",
    "bot:button:bot/unified_dashboard.py:DashboardControlView.filter_open",
    "bot:button:bot/unified_dashboard.py:DashboardControlView.sort_created",
    "bot:button:bot/unified_dashboard.py:DashboardControlView.sort_stale",
    "bot:button:bot/unified_dashboard.py:DashboardControlView.sort_team",
    "bot:button:bot/utils.py:CancelButton",
    "bot:button:bot/utils.py:ConfirmButton",
    "bot:command:bot/cogs/admin.py:AdminCog.admin_sync_roles",
    "bot:command:bot/cogs/admin_competition.py:AdminCompetitionCog.admin_broadcast",
    "bot:command:bot/cogs/admin_competition.py:AdminCompetitionCog.admin_cleanup_competition",
    "bot:command:bot/cogs/admin_competition.py:AdminCompetitionCog.admin_competition_set_apps",
    "bot:command:bot/cogs/admin_competition.py:AdminCompetitionCog.admin_reset_blueteam_passwords",
    "bot:command:bot/cogs/admin_competition.py:AdminCompetitionCog.admin_set_end_time",
    "bot:command:bot/cogs/admin_competition.py:AdminCompetitionCog.admin_set_max_members",
    "bot:command:bot/cogs/admin_competition.py:AdminCompetitionCog.admin_set_start_time",
    "bot:command:bot/cogs/admin_competition.py:AdminCompetitionCog.admin_start_competition",
    "bot:command:bot/cogs/admin_competition.py:AdminCompetitionCog.admin_stop_competition",
    "bot:command:bot/cogs/admin_teams.py:AdminTeamsCog.admin_activate_teams",
    "bot:command:bot/cogs/admin_teams.py:AdminTeamsCog.admin_deactivate_teams",
    "bot:command:bot/cogs/admin_teams.py:AdminTeamsCog.admin_recreate_teams",
    "bot:command:bot/cogs/admin_teams.py:AdminTeamsCog.admin_remove_team",
    "bot:command:bot/cogs/admin_teams.py:AdminTeamsCog.admin_reset_team",
    "bot:command:bot/cogs/admin_teams.py:AdminTeamsCog.admin_team_info",
    "bot:command:bot/cogs/admin_teams.py:AdminTeamsCog.admin_teams",
    "bot:command:bot/cogs/admin_teams.py:AdminTeamsCog.admin_unlink",
    "bot:command:bot/cogs/admin_tickets.py:AdminTicketsCog.admin_change_category",
    "bot:command:bot/cogs/admin_tickets.py:AdminTicketsCog.admin_ticket_cancel",
    "bot:command:bot/cogs/admin_tickets.py:AdminTicketsCog.admin_ticket_clear",
    "bot:command:bot/cogs/admin_tickets.py:AdminTicketsCog.admin_ticket_create",
    "bot:command:bot/cogs/admin_tickets.py:AdminTicketsCog.admin_ticket_list",
    "bot:command:bot/cogs/admin_tickets.py:AdminTicketsCog.admin_ticket_reassign",
    "bot:command:bot/cogs/admin_tickets.py:AdminTicketsCog.admin_ticket_reopen",
    "bot:command:bot/cogs/admin_tickets.py:AdminTicketsCog.admin_ticket_resolve",
    "bot:command:bot/cogs/linking.py:LinkingCog.link_command",
    "bot:command:bot/cogs/scoring.py:ScoringCog.incident_report",
    "bot:command:bot/cogs/ticketing.py:TicketingCog.create_ticket",
    "bot:listener:bot/cogs/help_panels.py:HelpPanelsCog.on_message",
    "bot:listener:bot/cogs/ticketing.py:TicketingCog.on_message",
    "bot:listener:bot/cogs/ticketing.py:TicketingCog.on_message_delete",
    "bot:listener:bot/cogs/ticketing.py:TicketingCog.on_message_edit",
    "bot:modal:bot/cogs/help_panels.py:BoxResetModal",
    "bot:modal:bot/cogs/help_panels.py:ConsultationModal",
    "bot:modal:bot/cogs/help_panels.py:OtherModal",
    "bot:modal:bot/cogs/help_panels.py:ScoringServiceCheckModal",
    "bot:modal:bot/cogs/help_panels.py:ServiceScoringModal",
    "bot:modal:bot/ticket_dashboard.py:ResolveTicketModal",
    "bot:select:bot/cogs/help_panels.py:CategorySelect",
]

EXCEPTIONS: dict[str, str] = {
    **dict.fromkeys(BOT_FOR_PR_C, PR_C),
    # The bot's ticket thread listener reads an attachment's bytes to copy it to the web ticket
    "bot/cogs/ticketing.py:TicketingCog.on_message": PR_C,
}


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
    registry = test_properties_parsers.PARSERS
    uncovered = [Unused(key, reasons) for key, reasons in found.items() if key not in registry]
    parser_exceptions = {k: v for k, v in EXCEPTIONS.items() if not k.startswith(("url:", "admin:", "bot:"))}
    fail_with("Parsers of uploads or CSV without a property test (add them to PARSERS)", uncovered, parser_exceptions)
    stale = sorted(set(registry) - set(found))
    assert not stale, f"PARSERS lists functions that no longer parse uploads or CSV: {stale}"
    not_properties = [
        f"{key}: {test.__name__}"
        for key, tests in registry.items()
        for test in tests
        if not getattr(test, "is_hypothesis_test", False)
    ]
    assert not not_properties, f"PARSERS lists tests that aren't Hypothesis property tests: {not_properties}"


# Each runs its properties for every row of its module's CASES, so a row covers its URL pattern (or admin model)
VIEW_MODULES = (
    test_properties_views_admin_site,
    test_properties_views_core,
    test_properties_views_orange,
    test_properties_views_packets,
    test_properties_views_registration,
    test_properties_views_scoring,
    test_properties_views_ticketing,
)


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
        for test in (module.test_any_request_is_answered_safely, module.test_every_field_is_escaped_where_echoed):
            cases = [m.args[1] for m in getattr(test, "pytestmark", []) if m.name == "parametrize"]
            assert cases == [module.CASES], f"{module.__name__}.{test.__name__} must run every row of CASES"


def test_every_bot_entry_point_is_covered_or_left_for_pr_c() -> None:
    found = bot_entry_points()
    fail_with(
        "Bot commands, modals, buttons and listeners without property tests",
        [Unused(key, where) for key, where in found.items()],
        {k: v for k, v in EXCEPTIONS.items() if k.startswith("bot:")},
    )


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
    """The Hypothesis profile (ci or deep, conftest.py) sets how many examples a test runs; a test that pins
    max_examples would run the same few under HYPOTHESIS_PROFILE=deep."""
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
