"""Guard: views raise PermissionDenied, Http404 and BadRequest, and core.errors answers them (the error page or JSON).
A view that builds a refusal itself (renders error.html, or makes a 400/403/404/405 answer) answers it its own way,
whoever asked: a fetch gets a page, a navigation gets JSON or bare text."""

import ast
import fnmatch

from core.tests.guards import REPO_DIR, fail_with, tracked_files

# The modules scanned (repo-relative fnmatch patterns over tracked files): the views
VIEW_PATTERNS = ("web/*/views.py", "web/*/views/*.py", "web/*/admin_views/*.py", "web/core/admin.py")

ERROR_STATUSES = {400, 403, 404, 405}
# Django's answer classes for those statuses
ERROR_CLASSES = {"HttpResponseBadRequest", "HttpResponseForbidden", "HttpResponseNotFound", "HttpResponseNotAllowed"}

# "path:function" -> why that view builds its error answer itself
EXCEPTIONS: dict[str, str] = {
    "web/scoring/views/red_team.py:ip_pool_create": (
        "the finding form's pool modal shows each field's errors: a 400 carrying the form's errors, not one reason"
    ),
}


def _name(func: ast.expr) -> str:
    return func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""


def hand_built_error(call: ast.Call) -> str:
    """What makes the call a hand-built error answer, or ""."""
    name = _name(call.func)
    if name in ERROR_CLASSES:
        return name
    if name == "render" and any(isinstance(a, ast.Constant) and a.value == "error.html" for a in call.args):
        return 'render("error.html")'
    if name in ("JsonResponse", "HttpResponse", "render"):
        for keyword in call.keywords:
            value = keyword.value
            if keyword.arg == "status" and isinstance(value, ast.Constant) and value.value in ERROR_STATUSES:
                return f"{name}(status={value.value})"
    return ""


def hand_built_errors(source: str, path: str) -> dict[str, str]:
    """Each hand-built error answer in the module, keyed by "path:function" for each def holding it."""
    found: dict[str, str] = {}
    for top in ast.walk(ast.parse(source)):
        if not isinstance(top, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for node in ast.walk(top):
            if isinstance(node, ast.Call) and (what := hand_built_error(node)):
                found.setdefault(f"{path}:{top.name}", f"line {node.lineno}, {what}")
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
def fine(request):
    return JsonResponse({"ok": True}, status=200)
def also_fine(request):
    return render(request, "page.html", {}, status=429)
"""
    assert set(hand_built_errors(source, "m.py")) == {f"m.py:{name}" for name in "abcde"}
