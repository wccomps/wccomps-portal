"""Guard: views raise PermissionDenied, Http404 and BadRequest, and core.errors answers them (the error page or JSON).
A view that builds a refusal itself (renders error.html, or makes a 400/403/404/405 answer) answers it its own way,
whoever asked: a fetch gets a page, a navigation gets JSON or bare text."""

import ast
import fnmatch

from core.tests.guards import REPO_DIR, fail_with, tracked_files

# The modules scanned (repo-relative fnmatch patterns over tracked files): the views
VIEW_PATTERNS = (
    "web/*/views.py",
    "web/*/views/*.py",
    "web/*/admin_views/*.py",
    "web/core/admin.py",
    "web/core/oauth.py",
)

ERROR_STATUSES = {400, 403, 404, 405}
# Django's answer classes for those statuses
ERROR_CLASSES = {"HttpResponseBadRequest", "HttpResponseForbidden", "HttpResponseNotFound", "HttpResponseNotAllowed"}

# "path:function" -> why that view builds its error answer itself
EXCEPTIONS: dict[str, str] = {
    "web/scoring/views/red_team.py:ip_pool_create": (
        "the finding form's pool modal shows each field's errors: a 400 carrying the form's errors, not one reason"
    ),
    "web/core/oauth.py:*": (
        "the sign-in flow's own page says what went wrong and how to start again, to someone not signed in yet"
    ),
    "web/core/views.py:link_*": (
        "the Discord link flow's own page says what went wrong and how to get a new link from Discord"
    ),
    "web/ticketing/views/create.py:form_page": (
        "re-renders the ticket form with what was posted and why it was refused (429 when the team is rate limited)"
    ),
}


def _name(func: ast.expr) -> str:
    return func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""


# Where each answer takes its template and its status, when passed by position
TEMPLATE_AT = {"render": 1, "TemplateResponse": 1}
STATUS_AT = {"render": 4, "TemplateResponse": 4, "HttpResponse": 2, "JsonResponse": None}


def _argument(call: ast.Call, name: str, position: int | None) -> ast.expr | None:
    for keyword in call.keywords:
        if keyword.arg == name:
            return keyword.value
    return call.args[position] if position is not None and len(call.args) > position else None


def hand_built_error(call: ast.Call) -> str:
    """What makes the call a hand-built error answer, or "": an error answer class, an error template (error.html
    and the pages named *error*), an error status, or a status the source doesn't spell out (a variable could hold
    one)."""
    name = _name(call.func)
    if name in ERROR_CLASSES:
        return name
    if name in TEMPLATE_AT:
        template = _argument(call, "template_name", TEMPLATE_AT[name])
        if isinstance(template, ast.Constant) and "error" in str(template.value).rsplit("/", 1)[-1]:
            return f"{name}({template.value!r})"
    if name in STATUS_AT:
        status = _argument(call, "status", STATUS_AT[name])
        if isinstance(status, ast.Constant) and status.value in ERROR_STATUSES:
            return f"{name}(status={status.value})"
        if status is not None and not isinstance(status, ast.Constant):
            return f"{name}(status={ast.unparse(status)})"
    return ""


def hand_built_errors(source: str, path: str) -> dict[str, str]:
    """Each hand-built error answer in the module, keyed by "path:function" for the innermost def holding it."""
    found: dict[str, str] = {}

    def visit(node: ast.AST, function: str) -> None:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            function = node.name
        elif function and isinstance(node, ast.Call) and (what := hand_built_error(node)):
            found.setdefault(f"{path}:{function}", f"line {node.lineno}, {what}")
        for child in ast.iter_child_nodes(node):
            visit(child, function)

    visit(ast.parse(source), "")
    return found


def test_views_raise_errors_instead_of_building_them() -> None:
    found: dict[str, str] = {}
    for path in tracked_files():
        if any(fnmatch.fnmatch(path, pattern) for pattern in VIEW_PATTERNS):
            found |= hand_built_errors((REPO_DIR / path).read_text(encoding="utf-8"), path)
    fail_with(
        "Views building an error answer (raise PermissionDenied, Http404 or BadRequest; core.errors answers it)",
        found,
        EXCEPTIONS,
    )


def test_the_guard_finds_each_shape() -> None:
    source = """
def a(request):
    return render(request, "error.html", {}, status=403)
def b(request):
    return JsonResponse({"error": "x"}, status=404)
def c(request):
    return HttpResponse("Method not allowed", status=405)
def d(request):
    return HttpResponseForbidden("no")
def e(request):
    return JsonResponse({"error": "x"}, status=400)
def f(request):
    return render(request, "page.html", {}, None, 404)
def g(request):
    return HttpResponse("no", "text/plain", 403)
def h(request, status):
    return JsonResponse({"error": "x"}, status=status)
def i(request):
    return TemplateResponse(request, "page.html", {}, status=400)
def j(request):
    return render(request, "core/oauth_error.html", {})
def k(request):
    return TemplateResponse(request, "link_error.html", {})
def fine(request):
    return JsonResponse({"ok": True}, status=200)
def also_fine(request):
    return render(request, "page.html", {}, status=429)
"""
    assert set(hand_built_errors(source, "m.py")) == {f"m.py:{name}" for name in "abcdefghijk"}
