import logging
import time
import zoneinfo
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

from django.conf import settings
from django.db import connection
from django.http import HttpRequest, HttpResponse, HttpResponsePermanentRedirect, JsonResponse
from django.shortcuts import redirect
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme

logger = logging.getLogger("wccomps.access")
error_logger = logging.getLogger("wccomps.errors")


class SecurityHeadersMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        response = self.get_response(request)

        if "Content-Security-Policy" not in response:
            response["Content-Security-Policy"] = (
                "default-src 'self'; "
                "script-src 'self' 'unsafe-inline' "
                "https://static.cloudflareinsights.com; "  # Alpine/htmx are vendored in static/vendor
                "style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data:; "
                "font-src 'self'; "
                "connect-src 'self'; "
                "frame-ancestors 'none'; "
                "form-action 'self'; "
                "base-uri 'self'"
            )

        return response


class UserTimezoneMiddleware:
    """Show and read times in the browser's timezone, which static/js/utils.js puts in the tz cookie.

    Without the cookie (first page view, scripts off) times stay in UTC, TIME_ZONE.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        try:
            timezone.activate(zoneinfo.ZoneInfo(request.COOKIES["tz"]))
        except KeyError, ValueError, zoneinfo.ZoneInfoNotFoundError:
            timezone.deactivate()
        return self.get_response(request)


class SubdomainRedirectMiddleware:
    """Redirect legacy hosts to the canonical host, and subdomain root paths to their app paths."""

    # OAuth callbacks must complete on the host whose redirect_uri Authentik issued the code for,
    # otherwise the token exchange fails for logins that were in flight on a legacy host.
    CANONICAL_EXEMPT_PATHS = frozenset({"/auth/callback/"})

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response
        self.subdomain_redirects: dict[str, str] = getattr(
            settings, "SUBDOMAIN_REDIRECTS", {"register.wccomps.org": "/register/"}
        )
        self.canonical_host: str = getattr(settings, "CANONICAL_HOST", "")
        self.legacy_hosts: frozenset[str] = frozenset(getattr(settings, "LEGACY_HOSTS", []))

    def __call__(self, request: HttpRequest) -> HttpResponse:
        host = request.get_host().split(":")[0]

        if self.canonical_host and host in self.legacy_hosts:
            if request.path in self.CANONICAL_EXEMPT_PATHS:
                self._log_legacy_use("legacy-host-callback", host, request, "-")
                return self.get_response(request)
            if request.path == "/" and host in self.subdomain_redirects:
                query = request.META.get("QUERY_STRING", "")
                target = self.subdomain_redirects[host] + (f"?{query}" if query else "")
            else:
                target = request.get_full_path()
            # 301 for GET/HEAD; 308 for everything else so the method and body survive
            preserve = request.method not in ("GET", "HEAD")
            response = HttpResponsePermanentRedirect(
                f"https://{self.canonical_host}{target}", preserve_request=preserve
            )
            self._log_legacy_use("legacy-host-redirect", host, request, str(response.status_code))
            return response

        if request.path == "/" and host in self.subdomain_redirects:
            return redirect(self.subdomain_redirects[host])
        return self.get_response(request)

    @staticmethod
    def _log_legacy_use(event: str, host: str, request: HttpRequest, status: str) -> None:
        """Record legacy-host traffic so retiring those hosts can be based on data.

        Paths only: query strings on these URLs can carry link tokens and OAuth codes.
        """
        referer = request.META.get("HTTP_REFERER", "").split("?", 1)[0] or "-"
        logger.info('%s %s "%s %s" %s %s', event, host, request.method, request.path, status, referer)


class AuthentikRequiredMiddleware:
    """Require Authentik login for all pages except OAuth flow and Discord linking."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

        self.whitelist_prefixes = [
            "/static/",
        ]
        self.whitelist_exact = [
            "/health/",  # Health check endpoint for monitoring
            "/livez/",  # Liveness probe (no database)
            "/register/",  # Public registration form
            "/auth/login/",
            "/auth/callback/",
            "/auth/logout/",
            "/auth/link",  # Discord account linking (token-based)
        ]
        self.whitelist_startswith = [
            "/register/edit/",  # Token-based registration editing
        ]

    def __call__(self, request: HttpRequest) -> HttpResponse:
        for prefix in self.whitelist_prefixes:
            if request.path.startswith(prefix):
                return self.get_response(request)
        if request.path in self.whitelist_exact:
            return self.get_response(request)
        for prefix in self.whitelist_startswith:
            if request.path.startswith(prefix):
                return self.get_response(request)

        if not request.user.is_authenticated:
            # The login redirect ends at Authentik, another origin, which a fetch or htmx request can't
            # follow: the browser reports a network error. An htmx poll reloads the page instead, which
            # navigates through login; any other background request gets an error it can show.
            if request.headers.get("HX-Request") == "true":
                return HttpResponse(status=204, headers={"HX-Refresh": "true"})
            if request.headers.get("Sec-Fetch-Mode", "navigate") != "navigate":
                return JsonResponse({"error": "Your session expired. Reload the page to sign in again."}, status=401)
            # Validate and sanitize the next parameter to prevent open redirect
            next_url = request.path
            if not url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
                next_url = "/"
            safe_next = quote(next_url, safe="")
            return redirect(f"/auth/login/?next={safe_next}")

        # Django admin requires Authentik admin permission
        if request.path.startswith("/admin/"):
            from .auth_utils import has_permission

            if not has_permission(request.user, "admin"):
                return HttpResponse("Forbidden", status=403)

        return self.get_response(request)


class QueryTracker:
    """Tracks database queries during a request.

    Implements Django's _ExecuteWrapper protocol from django-stubs.
    """

    def __init__(self) -> None:
        self.queries: list[float] = []  # durations in ms

    # Signature matches Django's _ExecuteWrapper type alias which uses Any
    def __call__(  # type: ignore[explicit-any]
        self,
        execute: Callable[[str, Any, bool, dict[str, Any]], Any],
        sql: str,
        params: Any,
        many: bool,
        context: dict[str, Any],
    ) -> Any:
        start = time.time()
        result = execute(sql, params, many, context)
        self.queries.append((time.time() - start) * 1000)
        return result


class AccessLoggingMiddleware:
    """Log all requests with username, response status, and query metrics."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if request.path.startswith("/static/"):
            return self.get_response(request)

        start_time = time.time()
        tracker = QueryTracker()

        try:
            with connection.execute_wrapper(tracker):
                response = self.get_response(request)
        except Exception:
            duration_ms = (time.time() - start_time) * 1000
            username = request.user.username if request.user.is_authenticated else "-"
            error_logger.error(
                '%s %s %s "%s %s" 500 %.0fms (unhandled exception)',
                request.META.get("REMOTE_ADDR", "-"),
                username,
                request.META.get("HTTP_HOST", "-"),
                request.method,
                request.path,
                duration_ms,
                exc_info=True,
            )
            raise

        duration_ms = (time.time() - start_time) * 1000
        username = request.user.username if request.user.is_authenticated else "-"

        # Log with query stats: [query_count, total_db_time_ms]
        log_msg = '%s %s %s "%s %s" %d %.0fms [%dq %.0fms]'
        log_args = (
            request.META.get("REMOTE_ADDR", "-"),
            username,
            request.META.get("HTTP_HOST", "-"),
            request.method,
            request.path,
            response.status_code,
            duration_ms,
            len(tracker.queries),
            sum(tracker.queries),
        )
        logger.info(log_msg, *log_args)

        if response.status_code >= 500:
            error_logger.error(log_msg, *log_args)

        return response
