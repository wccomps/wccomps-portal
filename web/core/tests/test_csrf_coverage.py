"""Every state-changing request path carries a CSRF token.

Static checks over templates (cover pages no other test renders) plus configuration checks.
PR #41 fixed ops pages that posted via wcPost but never set the csrftoken cookie.
"""

import re
from collections.abc import Iterable
from pathlib import Path

from django.conf import settings
from django.urls import URLPattern, URLResolver, get_resolver

from core.tests.guards import (
    TEMPLATES_DIR,
    elements,
    extends_chain,
    fail_with,
    scripts,
    static_scripts,
    text,
    tracked_templates,
)

# Renders the token in <meta name="csrf-token">, which also sets the csrftoken cookie
TOKEN_BASE_TEMPLATE = "admin/base_site.html"

# View callbacks allowed to skip CSRF, with the reason. Keep empty unless unavoidable.
CSRF_EXEMPT_ALLOWLIST: dict[str, str] = {}

JS_POST_RE = re.compile(r"\b(wcPost|wcStream)\s*\(|method\s*:\s*['\"]POST['\"]", re.IGNORECASE)
# Whole fetch(...) statement, so headers after `method` are seen; filtered to POSTs below
FETCH_CALL_RE = re.compile(r"fetch\([^;]*;", re.DOTALL)
POST_METHOD_RE = re.compile(r"method\s*:\s*['\"]POST['\"]", re.IGNORECASE)


POST_FORM_EXCEPTIONS = {"cotton/form.html:*": "the component itself; its callers put the token in its slot"}


def missing_form_tokens(paths: Iterable[Path], root: Path = TEMPLATES_DIR) -> dict[str, str]:
    """Each POST form (<form method="post">, or <c-form>, which always posts) whose body has no {% csrf_token %}."""
    missing = {}
    for path in paths:
        lines = text(path).splitlines()
        open_forms: list[tuple[str, int, bool]] = []
        for tag in elements(path):
            if tag.name not in ("form", "c-form"):
                continue
            if not tag.closing:
                open_forms.append(
                    (tag.name, tag.line, tag.name == "c-form" or tag.attrs.get("method", "").lower() == "post")
                )
                continue
            while open_forms:
                opened, line, posts = open_forms.pop()
                if opened == tag.name:
                    if posts and "csrf_token" not in "\n".join(lines[line - 1 : tag.line]):
                        missing[f"{path.relative_to(root).as_posix()}:{line}"] = "POST form without {% csrf_token %}"
                    break
    return missing


def js_post_problems(paths: Iterable[Path], root: Path = TEMPLATES_DIR) -> dict[str, str]:
    """Each template whose scripts post but that doesn't extend the token's base template, and each fetch POST in a
    script without an X-CSRFToken header."""
    problems = {}
    for path in paths:
        name = path.relative_to(root).as_posix()
        if not any(JS_POST_RE.search(js) for _, js in scripts(path)):
            continue
        if TOKEN_BASE_TEMPLATE not in extends_chain(path, root):
            problems[name] = f"posts from JavaScript but doesn't extend {TOKEN_BASE_TEMPLATE}"
        for line, js in scripts(path):
            for match in fetch_posts(js):
                if "X-CSRFToken" not in match.group(0):
                    problems[f"{name}:{line + js.count(chr(10), 0, match.start())}"] = "fetch POST without X-CSRFToken"
    return problems


def fetch_posts(source: str) -> list[re.Match[str]]:
    return [m for m in FETCH_CALL_RE.finditer(source) if POST_METHOD_RE.search(m.group(0))]


def _all_patterns(resolver: URLResolver, prefix: str = "") -> list[tuple[str, URLPattern]]:
    patterns = []
    for entry in resolver.url_patterns:
        if isinstance(entry, URLResolver):
            patterns += _all_patterns(entry, prefix + str(entry.pattern))
        else:
            patterns.append((prefix + str(entry.pattern), entry))
    return patterns


# --- The checks ---------------------------------------------------------------------------------


def test_every_post_form_has_csrf_token():
    # The standalone documents too: the public registration form posts
    fail_with("POST forms without {% csrf_token %}", missing_form_tokens(tracked_templates()), POST_FORM_EXCEPTIONS)


def test_javascript_posts_can_find_the_token():
    fail_with("Pages posting from JavaScript", js_post_problems(tracked_templates()), {})


def test_static_js_fetch_posts_send_csrf_header():
    problems = {
        f"{name}:{js.count(chr(10), 0, m.start()) + 1}": "fetch POST without X-CSRFToken"
        for name, js in static_scripts()
        for m in fetch_posts(js)
        if "X-CSRFToken" not in m.group(0)
    }
    fail_with("Static JavaScript posts", problems, {})


def test_token_base_template_renders_token():
    assert "{{ csrf_token }}" in (TEMPLATES_DIR / TOKEN_BASE_TEMPLATE).read_text()


def test_no_unexpected_csrf_exempt_views():
    exempt = {}
    for route, pattern in _all_patterns(get_resolver()):
        callback = pattern.callback
        if getattr(callback, "csrf_exempt", False):
            exempt[f"{callback.__module__}.{getattr(callback, '__qualname__', callback)}"] = route
    fail_with("csrf_exempt views", exempt, CSRF_EXEMPT_ALLOWLIST)


def test_csrf_middleware_enabled():
    assert "django.middleware.csrf.CsrfViewMiddleware" in settings.MIDDLEWARE


# --- The checks must catch what they claim to --------------------------------------------------


def plant(tmp_path: Path, files: dict[str, str]) -> list[Path]:
    for name, source in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(source)
    return [tmp_path / name for name in files]


def test_checker_flags_form_without_token(tmp_path: Path):
    files = {
        "x.html": '<form method="post"><input name="a"></form>',
        "y.html": "<c-form><input></c-form>",
        "z.html": '<form {% if n > 0 %}class="a"{% endif %} method="post"><input name="a"></form>',
    }
    assert list(missing_form_tokens(plant(tmp_path, files), tmp_path)) == ["x.html:1", "y.html:1", "z.html:1"]


def test_checker_accepts_form_with_token_and_ignores_get_forms(tmp_path: Path):
    files = {"x.html": '<form method="POST">{% csrf_token %}</form><form method="get"></form>'}
    assert missing_form_tokens(plant(tmp_path, files), tmp_path) == {}


def test_checker_flags_js_post_page_outside_token_base(tmp_path: Path):
    files = {
        "admin/base_site.html": "{{ csrf_token }}",
        "ok.html": '{% extends "mid.html" %}<script>wcPost(url, {})</script>',
        "mid.html": '{% extends "admin/base_site.html" %}',
        "bad.html": '{% extends "standalone.html" %}<script>wcPost(url, {})</script>',
        "standalone.html": "<html></html>",
    }
    assert list(js_post_problems(plant(tmp_path, files), tmp_path)) == ["bad.html"]


def test_checker_ignores_a_post_in_a_comment_or_outside_a_script(tmp_path: Path):
    files = {
        "a.html": "<script>// wcPost(url, {})\n/* fetch(u, { method: 'POST' }); */</script>",
        "b.html": "<p>wcPost(url, {})</p>",
        "c.html": "<script>const u = 'https://example.org/'; wcPost(u, {})</script>",
    }
    assert list(js_post_problems(plant(tmp_path, files), tmp_path)) == ["c.html"]


def test_checker_flags_raw_fetch_post_without_header(tmp_path: Path):
    files = {
        "admin/base_site.html": "",
        "p.html": '{% extends "admin/base_site.html" %}\n<script>\nfetch(u, { method: "POST", body: b });</script>',
    }
    assert list(js_post_problems(plant(tmp_path, files), tmp_path)) == ["p.html:3"]


def test_checker_sees_header_after_method(tmp_path: Path):
    """Real pages put headers after `method`; the check must read the whole call."""
    files = {
        "admin/base_site.html": "",
        "p.html": '{% extends "admin/base_site.html" %}<script>\n'
        "fetch(u, {\n method: 'POST',\n headers: {'X-CSRFToken': getCSRFToken()},\n body: b\n});</script>",
    }
    assert js_post_problems(plant(tmp_path, files), tmp_path) == {}
