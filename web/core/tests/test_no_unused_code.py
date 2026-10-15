"""Code nothing uses fails these tests, so leftovers can't build up between reviews that only read diffs.

Each test lists every unused item with where to find it, so a failure reads as a to-do list. Delete the item, or add
it to that test's exceptions with a one-line reason: who uses it from outside this repo, or why it must stay. An
exception that no longer matches anything fails too.

"Used" means read by the project's own source: tracked .py files and templates, minus tests, migrations and vendored
files. Code that only tests reach counts as unused. The checks share one index of the names source reads
(read_names): vulture's AST view of Python, plus names in template tags, quoted names and queryset lookups.
"""

import ast
import re
from functools import cache
from pathlib import Path
from typing import NamedTuple

import vulture
from django.apps import apps
from django.conf import global_settings
from django.db import models
from django.urls import URLPattern, URLResolver, get_resolver

from core import discord_tasks
from core.tests import guards

REPO_DIR = guards.REPO_DIR
WEB_DIR = guards.WEB_DIR
SETTINGS_FILE = "web/portal/settings.py"

SOURCE_SUFFIXES = (".py", ".html", ".txt", ".js")
NOT_SOURCE_RE = re.compile(
    r"^docs/|/migrations/|(^|/)tests/|^web/browser_tests/|(^|/)test_[^/]*\.py$|(^|/)conftest\.py$|^web/static/vendor/"
)


@cache
def source_files() -> tuple[str, ...]:
    """Tracked project source (repo-relative paths), without tests, migrations and vendored files."""
    return tuple(p for p in guards.tracked_files() if p.endswith(SOURCE_SUFFIXES) and not NOT_SOURCE_RE.search(p))


@cache
def source_text(path: str) -> str:
    return (REPO_DIR / path).read_text(encoding="utf-8")


@cache
def python_trees() -> dict[str, ast.Module]:
    return {p: ast.parse(source_text(p), filename=p) for p in source_files() if p.endswith(".py")}


def template_texts() -> dict[str, str]:
    return {p: source_text(p) for p in source_files() if p.startswith("web/templates/")}


class Unused(NamedTuple):
    key: str  # what the failure shows and what exceptions match
    where: str
    also: tuple[str, ...] = ()  # more keys exceptions may match, e.g. the item's decorators


def fail_with(title: str, unused: list[Unused], exceptions: dict[str, str]) -> None:
    """Fail listing the unused items no exception covers, plus exceptions that cover nothing (guards.fail_with)."""
    guards.fail_with(title, {u.key: u.where for u in unused}, exceptions, also={u.key: u.also for u in unused})


# ---------------------------------------------------------------------------------------------------------------
# The index of names source reads
# ---------------------------------------------------------------------------------------------------------------

IDENTIFIER_RE = re.compile(r"\w+")
DOTTED_NAME_RE = re.compile(r"[A-Za-z_][\w.]*")
# Template tags and variables, and cotton's :attr="expression" attributes
TEMPLATE_EXPRESSION_RE = re.compile(r"""{{.*?}}|{%.*?%}|\s:[\w-]+="[^"]*\"""", re.DOTALL)

# Calls whose keyword arguments are lookups (filter(team=x) reads team); other keywords (create, update) write
LOOKUP_CALLS = {
    "filter", "exclude", "get", "aget", "get_or_create", "aget_or_create", "update_or_create",
    "aupdate_or_create", "Q", "get_object_or_404", "get_list_or_404",
}  # fmt: skip
# Keywords that name fields only to write them: save(update_fields=[...]), update_or_create(defaults={...})
WRITE_KEYWORDS = {"update_fields", "defaults", "create_defaults"}
# Calls whose first argument is an environment variable name, not a Python name
ENV_READERS = {"os.environ.get", "os.getenv", "environ.get"}


@cache
def vulture_scan() -> vulture.Vulture:
    scanner = vulture.Vulture()
    scanner.scavenge([str(REPO_DIR / p) for p in python_trees()])
    return scanner


def call_name(node: ast.Call) -> str:
    return node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")


@cache
def template_names() -> frozenset[str]:
    """Identifiers in template expressions, where templates read fields, properties and methods."""
    return frozenset(
        word
        for text in template_texts().values()
        for expression in TEMPLATE_EXPRESSION_RE.findall(text)
        for word in IDENTIFIER_RE.findall(expression)
    )


@cache
def lookup_names() -> frozenset[str]:
    """Every name in a lookup keyword: team__name__in=x reads team and name."""
    return frozenset(
        part
        for tree in python_trees().values()
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and call_name(node) in LOOKUP_CALLS
        for keyword in node.keywords
        if keyword.arg
        for part in keyword.arg.split("__")
    )


def write_only_strings(path: str, tree: ast.Module) -> set[int]:
    """ids of string constants that name something without reading it.

    Fields listed to be written (update_fields, defaults), environment variable names, and a model's own Meta
    (an index, constraint or ordering on a field nothing reads doesn't make it read).
    """
    skipped: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg in WRITE_KEYWORDS:
            skipped.update(id(n) for n in ast.walk(node.value))
        elif isinstance(node, ast.Call) and node.args and ast.unparse(node.func) in ENV_READERS:
            skipped.update(id(n) for n in ast.walk(node.args[0]))
        elif isinstance(node, ast.Subscript) and ast.unparse(node.value) == "os.environ":
            skipped.update(id(n) for n in ast.walk(node.slice))
        elif path.endswith("/models.py") and isinstance(node, ast.ClassDef) and node.name == "Meta":
            skipped.update(id(n) for n in ast.walk(node))
    return skipped


@cache
def quoted_names() -> frozenset[str]:
    """Names Python reads through strings: admin list_display, getattr, order_by("-x"), "team__name", dotted paths."""
    names: set[str] = set()
    for path, tree in python_trees().items():
        skipped = write_only_strings(path, tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skipped:
                text = node.value.lstrip("-")
                if DOTTED_NAME_RE.fullmatch(text):
                    names.update(re.split(r"\.|__", text))
    return frozenset(names)


@cache
def read_names() -> frozenset[str]:
    """Every name source reads: vulture's used names (loads, not stores), plus template, quoted and lookup names."""
    return frozenset(vulture_scan().used_names) | template_names() | quoted_names() | lookup_names()


def definition_line(cls: type, name: str) -> str:
    """path:line where `name` is assigned in the body of cls or a project base class."""
    for klass in cls.__mro__:
        path = f"web/{klass.__module__.replace('.', '/')}.py"
        for node in ast.walk(python_trees().get(path, ast.Module(body=[], type_ignores=[]))):
            if isinstance(node, ast.ClassDef) and node.name == klass.__name__:
                for stmt in node.body:
                    targets = stmt.targets if isinstance(stmt, ast.Assign) else [getattr(stmt, "target", None)]
                    if any(isinstance(t, ast.Name) and t.id == name for t in targets):
                        return f"{path}:{stmt.lineno}"
    return cls.__module__


# ---------------------------------------------------------------------------------------------------------------
# 1. Model fields
# ---------------------------------------------------------------------------------------------------------------

# "app_label.Model.field" -> why it stays although no project code reads it
FIELD_EXCEPTIONS = {
    "team.LinkAttempt.failure_reason": "Audit trail: linking records why an attempt failed, for admins to read in the "
    "Django admin",
    "registration.TeamRegistration.rejection_reason": "The reject form requires a reason and stores it, for admins to "
    "read in the Django admin; no portal page shows it",
}


def project_models() -> list[type[models.Model]]:
    return [m for m in apps.get_models() if Path(m._meta.app_config.path).is_relative_to(WEB_DIR)]


def test_every_model_field_is_read() -> None:
    """Every concrete field of the project's models is read: writes (assignments, create() and update() keywords,
    update_fields) and the model's own Meta don't count.

    The check goes by name, so a write-only field that shares its name with something read slips through.
    """
    reads = read_names()
    unused = [
        Unused(f"{model._meta.label}.{field.name}", definition_line(model, field.name))
        for model in project_models()
        for field in (*model._meta.concrete_fields, *model._meta.many_to_many)
        if not (field.primary_key and field.name == "id") and not {field.name, field.attname} & reads
    ]
    fail_with("Model fields nothing reads", unused, FIELD_EXCEPTIONS)


# ---------------------------------------------------------------------------------------------------------------
# 2. Python code
# ---------------------------------------------------------------------------------------------------------------

VULTURE_CONFIDENCE = 60

DJANGO_MODEL_OPTION = "Django reads this model Meta option by name"
DJANGO_ADMIN_OPTION = "Django admin reads this ModelAdmin or inline option by name"
DJANGO_FORM_OPTION = "Django reads this form option by name"

# fnmatch pattern over "path:name", or "@decorator" for anything with that decorator -> why it stays
CODE_EXCEPTIONS = {
    # discord.py registers these through their decorators or calls them by name
    "@*.command": "discord.py registers a slash command through its decorator",
    "@discord.ui.button": "discord.py calls a button callback when the button is pressed",
    "@*.before_loop": "discord.py runs it before its task loop starts",
    "*:cog_unload": "discord.py calls it when the cog unloads",
    "*:cog_load": "discord.py calls it when the cog loads",
    "*:setup_hook": "discord.py calls it while the bot starts",
    "*:on_*": "discord.py calls event listeners and modal callbacks by name",
    "*:transform": "discord.py calls an app command Transformer's transform",
    # Django
    "@admin.register": "Django admin registers the class through its decorator",
    "*/apps.py:*Config": "Django loads the app config named in INSTALLED_APPS' app",
    "*/management/commands/*:Command": "manage.py runs a command by its file name",
    "*/management/commands/*:handle": "manage.py calls Command.handle",
    "*/management/commands/*:help": "manage.py help prints a command's help text",
    "*:clean_*": "Django form validation calls clean_<field>",
    "*:has_*_permission": "Django admin calls its permission hooks",
    "*:label_from_instance": "Django model choice fields call it for each option",
    "*:get_readonly_fields": "Django admin calls it on the change form",
    "*:abstract": DJANGO_MODEL_OPTION,
    "*:ordering": DJANGO_MODEL_OPTION,
    "*:db_table": DJANGO_MODEL_OPTION,
    "*:verbose_name": DJANGO_MODEL_OPTION,
    "*:verbose_name_plural": DJANGO_MODEL_OPTION,
    "*:constraints": DJANGO_MODEL_OPTION,
    "*:indexes": DJANGO_MODEL_OPTION,
    "*:unique_together": DJANGO_MODEL_OPTION,
    "*:default_auto_field": "Django reads AppConfig.default_auto_field",
    "*:list_display": DJANGO_ADMIN_OPTION,
    "*:list_filter": DJANGO_ADMIN_OPTION,
    "*:list_select_related": DJANGO_ADMIN_OPTION,
    "*:search_fields": DJANGO_ADMIN_OPTION,
    "*:autocomplete_fields": DJANGO_ADMIN_OPTION,
    "*:filter_horizontal": DJANGO_ADMIN_OPTION,
    "*:fieldsets": DJANGO_ADMIN_OPTION,
    "*:inlines": DJANGO_ADMIN_OPTION,
    "*:extra": DJANGO_ADMIN_OPTION,
    "*:widgets": DJANGO_FORM_OPTION,
    "*:labels": DJANGO_FORM_OPTION,
    "*:help_texts": DJANGO_FORM_OPTION,
    "*:empty_label": DJANGO_FORM_OPTION,
    "*/urls*.py:urlpatterns": "Django reads urlpatterns from each URLconf",
    "web/core/admin.py:site_*": "Django admin reads site_header, site_title and site_url off the AdminSite",
    "web/core/admin.py:index_title": "Django admin reads index_title off the AdminSite",
    "web/core/views.py:modified": "Setting request.session.modified tells Django to save the session",
    "web/gunicorn.conf.py:*": "gunicorn reads every top-level name in its config file",
    # Project code kept on purpose
    "web/scoring/models.py:validate_file_size": "scoring migration 0001 imports it as the screenshot validator; it "
    "can go once that migration is squashed",
    "web/scoring/snapshot.py:context_from_snapshot": "Reads back the scorecard snapshots Close Out stores in "
    "EventResult; no page shows a stored result yet",
}


@cache
def decorators_at() -> dict[tuple[str, int], tuple[str, ...]]:
    """(path, line) -> "@name" of each decorator on the def or class there, keyed by its first decorator's line."""
    found: dict[tuple[str, int], tuple[str, ...]] = {}
    for path, tree in python_trees().items():
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) and node.decorator_list:
                names = tuple("@" + ast.unparse(d.func if isinstance(d, ast.Call) else d) for d in node.decorator_list)
                found[path, min(d.lineno for d in node.decorator_list)] = names
    return found


def test_no_unused_python_code() -> None:
    """Functions, classes, methods, properties, attributes and module constants are used somewhere (vulture).

    vulture only reads Python, so a name in a template expression, a quoted name or a lookup counts too. Model
    fields and settings are left to their own tests.
    """
    extra_reads = template_names() | quoted_names() | lookup_names()
    field_names = {
        name for m in project_models() for f in m._meta.get_fields() for name in (f.name, getattr(f, "attname", ""))
    }
    unused = []
    for item in vulture_scan().get_unused_code(min_confidence=VULTURE_CONFIDENCE):
        path = Path(item.filename).relative_to(REPO_DIR).as_posix()
        if path == SETTINGS_FILE or item.name in extra_reads:
            continue
        if item.typ in ("attribute", "variable") and item.name in field_names:
            continue
        decorators = decorators_at().get((path, item.first_lineno), ())
        unused.append(Unused(f"{path}:{item.name}", f"line {item.first_lineno}, {item.typ}", decorators))
    fail_with("Python code nothing uses", unused, CODE_EXCEPTIONS)


# ---------------------------------------------------------------------------------------------------------------
# 3. Templates
# ---------------------------------------------------------------------------------------------------------------

# template path under web/templates -> who renders it without the project naming it
TEMPLATE_EXCEPTIONS = {
    "404.html": "Django's page-not-found handler renders it by name",
    "500.html": "Django's server-error handler renders it by name",
    "admin/index.html": "Overrides the Django admin index, which Django renders by name",
}


def template_reference_re(template: str) -> re.Pattern[str]:
    """How source names a template: its path in quotes, or a <c-...> tag for a cotton component."""
    quoted = rf"""["']{re.escape(template)}["']"""
    if not template.startswith("cotton/"):
        return re.compile(quoted)
    component = template.removeprefix("cotton/").removesuffix(".html").replace("/", ".")
    tag = re.escape(component).replace("_", "[-_]")
    return re.compile(rf"{quoted}|<c-{tag}[\s/>]")


def built_partials() -> set[str]:
    """Templates named at run time: the guide's section for each action, and the competition page's sections."""
    from core.admin_views.competition import guide_partial
    from core.admin_views.places import PLACES, Section, section_partial

    return {guide_partial(action) for action in PLACES} | {section_partial(section) for section in Section}


def test_every_template_is_used() -> None:
    """Every template under web/templates is rendered, included, extended or used as a cotton component."""
    built = built_partials()
    unused = []
    for path in template_texts():
        template = path.removeprefix("web/templates/")
        reference = template_reference_re(template)
        if template in built or any(reference.search(source_text(p)) for p in source_files() if p != path):
            continue
        unused.append(Unused(template, path))
    fail_with("Templates nothing renders", unused, TEMPLATE_EXCEPTIONS)


# ---------------------------------------------------------------------------------------------------------------
# 4. URL names
# ---------------------------------------------------------------------------------------------------------------

# URL name (with namespace) -> who requests it without the portal linking to it by name
URL_EXCEPTIONS = {
    "health_check": "The Kubernetes readiness probe and the uptime probe request /health/ by path",
    "livez": "Liveness probe (docker-compose.yml healthcheck, the Kubernetes deployment), requested by path",
    "oauth_callback": "Authentik redirects here after login (core/oauth.py sends the path as redirect_uri)",
    "link_initiate": "The bot DMs this path with a link token (bot/cogs/linking.py builds it from BASE_URL)",
    "ops_ticket_list_redirect": "Old /ops/tickets/ address, redirected for bookmarks; the Django admin's View site "
    "link (core/admin.py site_url) points here",
    "ops_ticket_detail_redirect": "Old /ops/ticket/<number>/ address, redirected for bookmarks",
    "scoring:leaderboard": "Old /scoring/ address of the leaderboard (now /leaderboard/), kept for bookmarks",
    "scoring:scorecard": "Old /scoring/ address of a scorecard (now under /leaderboard/), kept for bookmarks",
    "scoring:scorecard_pdf": "Old /scoring/ address of a scorecard PDF (now under /leaderboard/), kept for bookmarks",
    "scoring:orange_team_redirect": "Old orange team address, redirects to the orange team dashboard for bookmarks",
    "scoring:submit_orange_check": "Old orange check address, redirects to the orange team dashboard for bookmarks",
    "admin_task_status": "The Sync Roles page polls /ops/admin/task/<id>/status/ by path from its script",
}

PROJECT_MODULE_RE = re.compile(rf"^({'|'.join(p.name for p in WEB_DIR.iterdir() if (p / '__init__.py').exists())})\.")
# Calls that take a URL name as their first argument
URL_CALLS = {"reverse", "reverse_lazy", "redirect", "resolve_url"}
TEMPLATE_URL_RE = re.compile(r"""{%\s*url\s+["']([^"']+)["']""")


def project_url_names(resolver: URLResolver, namespace: str = "", prefix: str = "/") -> dict[str, str]:
    """Named URL patterns whose view is project code (Django admin's own URLs are left out), name -> full route."""
    names: dict[str, str] = {}
    for pattern in resolver.url_patterns:
        route = f"{prefix}{pattern.pattern}"
        if isinstance(pattern, URLResolver):
            inner = f"{namespace}{pattern.namespace}:" if pattern.namespace else namespace
            names |= project_url_names(pattern, inner, route)
        elif isinstance(pattern, URLPattern) and pattern.name and PROJECT_MODULE_RE.match(pattern.callback.__module__):
            names[f"{namespace}{pattern.name}"] = route
    return names


def linked_url_names() -> set[str]:
    """URL names source links to: {% url "x" %}, the first argument of reverse()/redirect()/..., and *url= keywords
    (bulk_approve(redirect_url="x") hands the name on to redirect)."""
    names = {name for text in template_texts().values() for name in TEMPLATE_URL_RE.findall(text)}
    for tree in python_trees().values():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            values = [node.args[0]] if node.args and call_name(node) in URL_CALLS else []
            values += [k.value for k in node.keywords if k.arg and k.arg.endswith("url")]
            names.update(v.value for v in values if isinstance(v, ast.Constant) and isinstance(v.value, str))
    return names


def test_every_url_name_is_linked() -> None:
    """Every named URL is linked by name somewhere: {% url %}, reverse, redirect, ..."""
    linked = linked_url_names()
    unused = [Unused(n, route) for n, route in project_url_names(get_resolver()).items() if n not in linked]
    fail_with("URL names nothing links to", unused, URL_EXCEPTIONS)


# ---------------------------------------------------------------------------------------------------------------
# 5. Settings
# ---------------------------------------------------------------------------------------------------------------

# Settings Django reads: every one it has a default for, plus ROOT_URLCONF, which has none
DJANGO_SETTINGS = {name for name in dir(global_settings) if name.isupper()} | {"ROOT_URLCONF"}

# setting -> why it stays although nothing reads it
SETTING_EXCEPTIONS: dict[str, str] = {}


def test_every_setting_is_read() -> None:
    """Every setting in portal/settings.py is read: by project code, by another setting, or by Django."""
    reads = read_names()
    unused = [
        Unused(target.id, f"{SETTINGS_FILE}:{node.lineno}")
        for node in python_trees()[SETTINGS_FILE].body
        if isinstance(node, ast.Assign | ast.AnnAssign)
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        if isinstance(target, ast.Name) and target.id.isupper()
        if target.id not in DJANGO_SETTINGS and target.id not in reads
    ]
    fail_with("Settings nothing reads", unused, SETTING_EXCEPTIONS)


# ---------------------------------------------------------------------------------------------------------------
# 6. Discord task payloads
# ---------------------------------------------------------------------------------------------------------------

# payload class -> why it stays although nothing enqueues it
PAYLOAD_EXCEPTIONS: dict[str, str] = {}


def test_every_task_payload_is_enqueued() -> None:
    """Each payload type in the TaskPayload union is built somewhere, so the bot doesn't handle a task nobody sends.

    The bot's dispatch matches every type (mypy checks that); this checks the other end. A `case Name()` pattern
    is not a call, so the dispatch itself doesn't count.
    """
    called = {call_name(n) for tree in python_trees().values() for n in ast.walk(tree) if isinstance(n, ast.Call)}
    unused = [
        Unused(cls.__name__, "web/core/discord_tasks.py")
        for cls in discord_tasks.PAYLOAD_TYPES.values()
        if cls.__name__ not in called
    ]
    fail_with("Discord task payloads nothing enqueues", unused, PAYLOAD_EXCEPTIONS)
