"""Code nothing uses fails these tests, so leftovers can't build up between reviews that only read diffs.

Each test lists every unused item with where it is defined, so a failure reads as a to-do list. Delete the item,
or add it to that test's exceptions dict with a one-line reason: who uses it from outside this repo, or why it
must stay. An exception that no longer matches anything fails too.

"Used" means referenced from the project's own source: tracked .py files, templates and static JS, minus tests,
migrations and vendored files. Code that only tests reach counts as unused.
"""

import ast
import re
import subprocess
from functools import cache
from pathlib import Path

import pytest
import vulture
from django.apps import apps
from django.conf import settings
from django.db import models
from django.urls import URLPattern, URLResolver, get_resolver

from core import discord_tasks

REPO_DIR = Path(__file__).resolve().parents[3]
WEB_DIR = REPO_DIR / "web"
SETTINGS_FILE = "web/portal/settings.py"
VULTURE_WHITELIST = Path(__file__).with_name("vulture_whitelist.py")

SOURCE_SUFFIXES = (".py", ".html", ".txt", ".js")
NOT_SOURCE_RE = re.compile(
    r"^docs/|/migrations/|(^|/)tests/|^web/browser_tests/|(^|/)test_[^/]*\.py$|(^|/)conftest\.py$|^web/static/vendor/"
)


@cache
def source_files() -> tuple[str, ...]:
    """Tracked project source (repo-relative paths), without tests, migrations and vendored files."""
    tracked = subprocess.run(
        ["git", "ls-files", "-z"],  # noqa: S607
        cwd=REPO_DIR,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\0")
    return tuple(p for p in tracked if p.endswith(SOURCE_SUFFIXES) and not NOT_SOURCE_RE.search(p))


@cache
def source_text(path: str) -> str:
    return (REPO_DIR / path).read_text(encoding="utf-8")


@cache
def corpus() -> str:
    """All source in one string, for searches that don't care which file matched."""
    return "\n".join(source_text(p) for p in source_files())


def fail_with(title: str, unused: list[str], exceptions: dict[str, str], found: set[str]) -> None:
    """Fail listing the unused items, plus exceptions that no longer match anything."""
    stale = [f"{key}: in the exceptions but no longer unused; drop it there" for key in exceptions if key not in found]
    problems = sorted(unused) + stale
    if problems:
        pytest.fail(f"{title} ({len(problems)}):\n" + "\n".join(f"  {p}" for p in problems), pytrace=False)


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

FIELD_DEFINITION_RE = re.compile(r"^\s*\w+\s*(:[^=\n]*)?=\s*(models\.\w+|\w+Field)\(", re.MULTILINE)
# Field names quoted only to write them: save(update_fields=[...]), update_or_create(defaults={...})
FIELD_WRITE_LIST_RE = re.compile(r"\b(update_fields\s*=\s*[\[(][^\])]*[\])]|(create_)?defaults\s*=\s*\{[^}]*\})")


def project_models() -> list[type[models.Model]]:
    return [m for m in apps.get_models() if Path(m._meta.app_config.path).is_relative_to(WEB_DIR)]


def project_field_names() -> set[str]:
    fields = (field for model in project_models() for field in model._meta.get_fields())
    return {name for field in fields for name in (field.name, getattr(field, "attname", ""))}


@cache
def corpus_without_field_definitions() -> str:
    """The corpus minus field definitions and update_fields / defaults lists, which name a field only to write it."""
    return FIELD_WRITE_LIST_RE.sub("", FIELD_DEFINITION_RE.sub("", corpus()))


# Calls whose keyword arguments are lookups (filter(team=x) reads team); other keywords (create, update) write
LOOKUP_CALLS = {
    "filter", "exclude", "get", "aget", "get_or_create", "aget_or_create", "update_or_create",
    "aupdate_or_create", "Q", "get_object_or_404", "get_list_or_404",
}  # fmt: skip


@cache
def lookup_keywords() -> frozenset[str]:
    """Every field name used in a lookup keyword (team__name__in=x reads team and name)."""
    names: set[str] = set()
    for path in source_files():
        if path.endswith(".py"):
            for node in ast.walk(ast.parse(source_text(path))):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
                if func in LOOKUP_CALLS:
                    names.update(part for kw in node.keywords if kw.arg for part in kw.arg.split("__"))
    return frozenset(names)


def field_read_re(name: str) -> re.Pattern[str]:
    """Text that reads a field called `name`; assignments and create()/update() keywords are writes.

    Reads: obj.name (not obj.name = / +=), the name quoted ("name", "-name", "team__name", "name__in": lookups,
    order_by, values(), admin and form field lists), and template variables and tags.
    """
    n = re.escape(name)
    return re.compile(
        rf"""\.{n}\b(?!\s*[-+*/|&]?=[^=])"""
        rf"""|["'](-|\w+__)?{n}(__\w+)?["']"""
        rf"""|{{{{[^}}]*\b{n}\b|{{%[^%]*\b{n}\b"""
    )


def test_every_model_field_is_read() -> None:
    """Every concrete field of the project's models is read somewhere outside migrations and tests.

    The check goes by name: a read of any field or attribute with the same name counts, so `name` passes as long
    as anything reads a `.name`. That keeps it simple; the cost is that a write-only field sharing its name with
    a read one slips through.
    """
    text = corpus_without_field_definitions()
    lookups = lookup_keywords()
    found: set[str] = set()
    unused: list[str] = []
    for model in project_models():
        for field in (*model._meta.concrete_fields, *model._meta.many_to_many):
            if field.primary_key and field.name == "id":
                continue
            names = {field.name, field.attname}
            if names & lookups or any(field_read_re(name).search(text) for name in names):
                continue
            key = f"{model._meta.label}.{field.name}"
            found.add(key)
            if key not in FIELD_EXCEPTIONS:
                where = Path(model._meta.app_config.path).relative_to(REPO_DIR) / "models.py"
                unused.append(f"{key} ({where})")
    fail_with("Model fields nothing reads", unused, FIELD_EXCEPTIONS, found)


# ---------------------------------------------------------------------------------------------------------------
# 2. Python code
# ---------------------------------------------------------------------------------------------------------------

VULTURE_CONFIDENCE = 60

# Callbacks a framework registers through their decorator
IGNORE_DECORATORS = [
    # discord.py: slash commands, listeners, buttons, background tasks
    "@app_commands.command",
    "@*.command",
    "@commands.Cog.listener",
    "@discord.ui.button",
    "@tasks.loop",
    "@*.before_loop",
    # Django admin registrations, actions and columns; template filters
    "@admin.register",
    "@admin.action",
    "@admin.display",
    "@register.filter",
    "@register.simple_tag",
]

# Names a framework calls or reads by convention
IGNORE_NAMES = [
    # discord.py: cog lifecycle, bot startup, modal and view callbacks, app command transformers
    "cog_load",
    "cog_unload",
    "setup_hook",
    "on_*",
    "transform",
    # Django: app configs, management commands, form validation, admin permission hooks
    "*Config",
    "ready",
    "Command",
    "handle",
    "add_arguments",
    "clean_*",
    "has_*_permission",
    "label_from_instance",
    "get_readonly_fields",
    # Django model Meta, ModelAdmin, inline and form options
    "Meta",
    "abstract",
    "ordering",
    "db_table",
    "verbose_name",
    "verbose_name_plural",
    "constraints",
    "indexes",
    "unique_together",
    "default_auto_field",
    "list_display",
    "list_filter",
    "list_select_related",
    "search_fields",
    "readonly_fields",
    "autocomplete_fields",
    "filter_horizontal",
    "fieldsets",
    "inlines",
    "actions",
    "extra",
    "widgets",
    "labels",
    "help_texts",
    "empty_label",
    "help",
    "urlpatterns",
    "app_name",
]

# "path:name" -> why it stays although nothing in the repo uses it
CODE_EXCEPTIONS = {
    "web/scoring/models.py:validate_file_size": "scoring migration 0001 imports it as the screenshot validator; it "
    "can go once that migration is squashed",
    "web/scoring/snapshot.py:context_from_snapshot": "Reads back the scorecard snapshots Close Out stores in "
    "EventResult; no page shows a stored result yet",
}

IDENTIFIER_RE = re.compile(r"\w+")
DOTTED_NAME_RE = re.compile(r"[A-Za-z_][\w.]*")
TEMPLATE_TAG_RE = re.compile(r"{{.*?}}|{%.*?%}", re.DOTALL)


@cache
def template_names() -> frozenset[str]:
    """Identifiers inside template tags and variables, where templates call methods and properties."""
    return frozenset(
        word
        for path in source_files()
        if path.startswith("web/templates/")
        for tag in TEMPLATE_TAG_RE.findall(source_text(path))
        for word in IDENTIFIER_RE.findall(tag)
    )


@cache
def quoted_python_names() -> frozenset[str]:
    """Names Python passes as strings: admin list_display, getattr, "team__name" lookups, dotted import paths."""
    strings = (
        node.value
        for path in source_files()
        if path.endswith(".py")
        for node in ast.walk(ast.parse(source_text(path)))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    )
    return frozenset(name for s in strings if DOTTED_NAME_RE.fullmatch(s) for name in re.split(r"\.|__", s))


def test_no_unused_python_code() -> None:
    """Functions, classes, methods, properties, attributes and module constants are used somewhere (vulture).

    vulture only reads Python, so a name used inside a template tag or passed as a string also counts. Model
    fields and settings are left to their own tests, which tell reads from writes.
    """
    scanner = vulture.Vulture(ignore_names=IGNORE_NAMES, ignore_decorators=IGNORE_DECORATORS)
    scanner.scavenge([*(str(REPO_DIR / p) for p in source_files() if p.endswith(".py")), str(VULTURE_WHITELIST)])
    used_elsewhere = template_names() | quoted_python_names()
    field_names = project_field_names()
    found: set[str] = set()
    unused: list[str] = []
    for item in scanner.get_unused_code(min_confidence=VULTURE_CONFIDENCE):
        path = Path(item.filename).relative_to(REPO_DIR).as_posix()
        if item.name in used_elsewhere or path == SETTINGS_FILE:
            continue
        if item.typ in ("attribute", "variable") and item.name in field_names:
            continue
        key = f"{path}:{item.name}"
        found.add(key)
        if key not in CODE_EXCEPTIONS:
            unused.append(f"{key} (line {item.first_lineno}, {item.typ})")
    fail_with("Python code nothing uses", unused, CODE_EXCEPTIONS, found)


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


def test_every_template_is_used() -> None:
    """Every template under web/templates is rendered, included, extended or used as a cotton component."""
    found: set[str] = set()
    unused: list[str] = []
    for path in source_files():
        if not path.startswith("web/templates/"):
            continue
        template = path.removeprefix("web/templates/")
        reference = template_reference_re(template)
        if any(reference.search(source_text(p)) for p in source_files() if p != path):
            continue
        found.add(template)
        if template not in TEMPLATE_EXCEPTIONS:
            unused.append(path)
    fail_with("Templates nothing renders", unused, TEMPLATE_EXCEPTIONS, found)


# ---------------------------------------------------------------------------------------------------------------
# 4. URL names
# ---------------------------------------------------------------------------------------------------------------

# URL name (with namespace) -> who requests it without the portal linking to it by name
URL_EXCEPTIONS = {
    "health_check": "Health endpoint for monitoring, requested by path",
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
    "scoring:api_team_detail": "Per-team JSON counterpart of api_scores (linked as JSON on the leaderboard), for "
    "scripts; callers outside the repo can't be ruled out",
}

PROJECT_MODULE_RE = re.compile(rf"^({'|'.join(p.name for p in WEB_DIR.iterdir() if (p / '__init__.py').exists())})\.")


def project_url_names(resolver: URLResolver, namespace: str = "") -> dict[str, str]:
    """Named URL patterns whose view is project code (Django admin's own URLs are left out), name -> route."""
    names: dict[str, str] = {}
    for pattern in resolver.url_patterns:
        if isinstance(pattern, URLResolver):
            names |= project_url_names(pattern, f"{namespace}{pattern.namespace}:" if pattern.namespace else namespace)
        elif isinstance(pattern, URLPattern) and pattern.name and PROJECT_MODULE_RE.match(pattern.callback.__module__):
            names[f"{namespace}{pattern.name}"] = str(pattern.pattern)
    return names


def test_every_url_name_is_linked() -> None:
    """Every named URL is reversed or linked somewhere ({% url %}, reverse, redirect, ...)."""
    text = corpus()
    found: set[str] = set()
    unused: list[str] = []
    for name, route in project_url_names(get_resolver()).items():
        if re.search(rf"""(?<!name=)["']{re.escape(name)}["']""", text):
            continue
        found.add(name)
        if name not in URL_EXCEPTIONS:
            unused.append(f"{name} ({route})")
    fail_with("URL names nothing links to", unused, URL_EXCEPTIONS, found)


# ---------------------------------------------------------------------------------------------------------------
# 5. Settings
# ---------------------------------------------------------------------------------------------------------------

# Settings Django reads by name
FRAMEWORK_SETTINGS = {
    "ALLOWED_HOSTS",
    "AUTH_PASSWORD_VALIDATORS",
    "CSRF_TRUSTED_ORIGINS",
    "DATABASES",
    "DEBUG",
    "DEFAULT_AUTO_FIELD",
    "DEFAULT_FROM_EMAIL",
    "EMAIL_BACKEND",
    "EMAIL_HOST",
    "EMAIL_HOST_PASSWORD",
    "EMAIL_HOST_USER",
    "EMAIL_PORT",
    "EMAIL_USE_SSL",
    "EMAIL_USE_TLS",
    "INSTALLED_APPS",
    "LANGUAGE_CODE",
    "LOGGING",
    "LOGIN_REDIRECT_URL",
    "LOGIN_URL",
    "LOGOUT_REDIRECT_URL",
    "MEDIA_ROOT",
    "MEDIA_URL",
    "MIDDLEWARE",
    "ROOT_URLCONF",
    "SECRET_KEY",
    "SECURE_PROXY_SSL_HEADER",
    "SERVER_EMAIL",
    "SESSION_COOKIE_AGE",
    "SESSION_COOKIE_HTTPONLY",
    "SESSION_COOKIE_SAMESITE",
    "SESSION_SAVE_EVERY_REQUEST",
    "STATICFILES_DIRS",
    "STATIC_ROOT",
    "STATIC_URL",
    "STORAGES",
    "TEMPLATES",
    "TIME_ZONE",
    "USE_I18N",
    "USE_TZ",
    "WSGI_APPLICATION",
}

# setting -> why it stays although nothing reads it
SETTING_EXCEPTIONS: dict[str, str] = {}


def defined_settings() -> dict[str, int]:
    """Top-level UPPER_CASE names assigned in settings.py, with their line."""
    defined: dict[str, int] = {}
    for node in ast.parse(source_text(SETTINGS_FILE)).body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Name) and target.id.isupper():
                defined.setdefault(target.id, node.lineno)
    return defined


def test_every_setting_is_read() -> None:
    """Every setting in portal/settings.py is read: by project code, by another setting, or by Django."""
    defined = defined_settings()
    settings_lines = source_text(SETTINGS_FILE).splitlines()
    found: set[str] = set()
    unused: list[str] = []
    for name, line in defined.items():
        if name in FRAMEWORK_SETTINGS:
            continue
        word = re.compile(rf"\b{name}\b")
        elsewhere = any(word.search(source_text(p)) for p in source_files() if p != SETTINGS_FILE)
        in_settings = any(word.search(text) for number, text in enumerate(settings_lines, 1) if number != line)
        if elsewhere or in_settings:
            continue
        found.add(name)
        if name not in SETTING_EXCEPTIONS:
            unused.append(f"{name} ({SETTINGS_FILE}:{line})")
    fail_with("Settings nothing reads", unused, SETTING_EXCEPTIONS, found)


def test_framework_settings_are_defined() -> None:
    """The allowlist names only settings that exist, so it can't hide a typo or outlive a removed setting."""
    assert sorted(FRAMEWORK_SETTINGS - set(defined_settings())) == []
    assert all(hasattr(settings, name) for name in FRAMEWORK_SETTINGS)


# ---------------------------------------------------------------------------------------------------------------
# 6. Discord task payloads
# ---------------------------------------------------------------------------------------------------------------

# payload class -> why it stays although nothing enqueues it
PAYLOAD_EXCEPTIONS: dict[str, str] = {}


def test_every_task_payload_is_enqueued() -> None:
    """Each payload type in the TaskPayload union is built somewhere, so the bot doesn't handle a task nobody sends.

    The bot's dispatch matches every type (mypy checks that); this checks the other end.
    """
    python = [source_text(p) for p in source_files() if p.endswith(".py")]
    found: set[str] = set()
    unused: list[str] = []
    for name in sorted(cls.__name__ for cls in discord_tasks.PAYLOAD_TYPES.values()):
        built = re.compile(rf"^(?!\s*(case|class)\b).*\b{name}\(", re.MULTILINE)
        if any(built.search(text) for text in python):
            continue
        found.add(name)
        if name not in PAYLOAD_EXCEPTIONS:
            unused.append(f"{name} (web/core/discord_tasks.py)")
    fail_with("Discord task payloads nothing enqueues", unused, PAYLOAD_EXCEPTIONS, found)
