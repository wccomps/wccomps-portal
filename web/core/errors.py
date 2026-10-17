"""One answer for every refusal and missing record.

Views raise PermissionDenied (signed in without the role), Http404 (missing, or another team's record) and BadRequest
(input the view refuses); the project's handlers (portal/urls.py) answer each, and a wrong method's 405, the same way:
the error page, or {"error": ...} with the same status for a JSON endpoint or a fetch() (wants_json_error). A
signed-out visitor is sent to sign in (sign_in_answer).
"""

from collections.abc import Callable
from typing import TYPE_CHECKING
from urllib.parse import quote

from django.core.exceptions import BadRequest, PermissionDenied
from django.http import HttpRequest, HttpResponse, HttpResponseNotAllowed, JsonResponse
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import defaults

if TYPE_CHECKING:
    from .auth_utils import ViewFunc


def json_endpoint[**P](view: ViewFunc[P]) -> ViewFunc[P]:
    """Mark a view as a JSON endpoint: its errors and refusals are JSON whoever calls it."""
    view.json_endpoint = True  # type: ignore[attr-defined]
    return view


def is_json_endpoint(view: object) -> bool:
    return getattr(view, "json_endpoint", False) is True


def wants_json_error(request: HttpRequest) -> bool:
    """Whether an answer is JSON: a JSON endpoint's always; another view's when the page's script asks for JSON
    (Accept: application/json, whatever the browser), or for a background fetch(), which can show it. A navigation
    gets a page, and htmx (which doesn't swap in an error answer; base_site.html alerts) gets the page too. Before the
    URL resolves (a middleware's answer) only the headers decide."""
    match = request.resolver_match
    if match is not None and is_json_endpoint(match.func):
        return True
    if request.headers.get("Accept", "").startswith("application/json"):
        return True
    return (
        request.headers.get("HX-Request") != "true" and request.headers.get("Sec-Fetch-Mode", "navigate") != "navigate"
    )


def sign_in_answer(request: HttpRequest) -> HttpResponse:
    """A signed-out visitor's answer: sent to sign in, coming back here after.

    The login redirect ends at Authentik, another origin, which a fetch or htmx request can't follow: the browser
    reports a network error. An htmx poll reloads the page instead, which navigates through login; any other
    background request gets an error it can show.
    """
    if request.headers.get("HX-Request") == "true":
        return HttpResponse(status=204, headers={"HX-Refresh": "true"})
    if wants_json_error(request):
        return JsonResponse({"error": "Your session expired. Reload the page to sign in again."}, status=401)
    next_url = request.path
    if not url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
        next_url = "/"
    return redirect(f"/auth/login/?next={quote(next_url, safe='')}")


TITLES = {400: "Bad request", 403: "No access", 404: "Not found", 405: "Not allowed", 500: "Server error"}
MESSAGES = {
    400: "The request couldn't be used.",
    403: "You don't have access to this page.",
    404: "Either red team took it down, or it never existed.",
    405: "This page doesn't take that kind of request.",
    500: "Something broke on our side. It has been logged.",
}


def error_answer(request: HttpRequest, status: int, message: str = "") -> HttpResponse:
    """The error page, or JSON for wants_json_error, with this status. The page links to the person's own landing
    page (the one a 403 sends them back to)."""
    from .auth_utils import get_authentik_groups, get_role_based_landing_url

    message = message or MESSAGES[status]
    if wants_json_error(request):
        return JsonResponse({"error": message}, status=status)
    home_url = get_role_based_landing_url(get_authentik_groups(request.user)) if hasattr(request, "user") else "/"
    context = {"error": TITLES[status], "message": message, "home_url": home_url if home_url != request.path else ""}
    return render(request, "error.html", context, status=status)


def _reason(exception: Exception, kind: type[Exception]) -> str:
    """The reason a view gave, for the exceptions views raise with one; never for another kind (a 404's reason
    would tell a missing record from another team's)."""
    return str(exception) if type(exception) is kind and exception.args else ""


def bad_request(request: HttpRequest, exception: Exception) -> HttpResponse:
    if not isinstance(exception, BadRequest) and not wants_json_error(request):
        # Django's own refusals (a disallowed host, a request too large) answer before the request is one a page can
        # be rendered for: its plain page
        return defaults.bad_request(request, exception)
    return error_answer(request, 400, _reason(exception, BadRequest))


def permission_denied(request: HttpRequest, exception: Exception) -> HttpResponse:
    return error_answer(request, 403, _reason(exception, PermissionDenied))


def page_not_found(request: HttpRequest, exception: Exception) -> HttpResponse:
    return error_answer(request, 404)


def csrf_failure(request: HttpRequest, reason: str = "") -> HttpResponse:
    """CSRF_FAILURE_VIEW: a form or fetch whose CSRF token didn't check out (most often a page open since before its
    session changed)."""
    return error_answer(request, 403, "Your session changed since this page loaded. Reload the page and try again.")


def server_error(request: HttpRequest) -> HttpResponse:
    if wants_json_error(request):
        return JsonResponse({"error": MESSAGES[500]}, status=500)
    return defaults.server_error(request)  # 500.html, a page of its own: rendering the portal's may be what failed


class MethodNotAllowedMiddleware:
    """Answer a wrong method (require_POST, require_http_methods) as every other error is: Django has no handler
    for a 405, and its own is an empty answer."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        response = self.get_response(request)
        if isinstance(response, HttpResponseNotAllowed):
            answer = error_answer(request, 405)
            answer["Allow"] = response["Allow"]
            return answer
        return response
