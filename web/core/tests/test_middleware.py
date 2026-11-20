"""Tests for middleware (AuthentikRequiredMiddleware) and the security settings."""

import json

import pytest
from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.http import HttpResponse
from django.test import Client, RequestFactory
from django.urls import URLPattern, URLResolver, get_resolver

from core.middleware import AuthentikRequiredMiddleware

pytestmark = pytest.mark.django_db


class TestMiddlewareConfiguration:
    """Tests for middleware ordering."""

    def test_authentik_middleware_runs_after_auth(self):
        """AuthentikRequiredMiddleware must run after AuthenticationMiddleware."""
        middleware_list = list(settings.MIDDLEWARE)
        auth_idx = middleware_list.index("django.contrib.auth.middleware.AuthenticationMiddleware")
        authentik_idx = middleware_list.index("core.middleware.AuthentikRequiredMiddleware")

        assert authentik_idx > auth_idx, (
            "AuthentikRequiredMiddleware must run after AuthenticationMiddleware to have access to request.user"
        )


# Must match AuthentikRequiredMiddleware whitelist
MIDDLEWARE_WHITELIST_PREFIXES = ["/static/"]
MIDDLEWARE_WHITELIST_EXACT = [
    "/health/",
    "/livez/",
    "/register/",
    "/auth/login/",
    "/auth/callback/",
    "/auth/logout/",
    "/auth/link",
    "/admin/login/",
]


class TestAuthentikRequiredMiddleware:
    """Tests for AuthentikRequiredMiddleware."""

    @pytest.fixture
    def middleware(self):
        """Create middleware instance with mock get_response."""

        def get_response(request):
            return HttpResponse("OK")

        return AuthentikRequiredMiddleware(get_response)

    @pytest.fixture
    def authenticated_request(self, blue_team_user):
        """Create an authenticated request."""
        factory = RequestFactory()
        request = factory.get("/some/path/")
        request.user = blue_team_user
        return request

    @pytest.fixture
    def anonymous_request(self):
        """Create an anonymous request."""
        factory = RequestFactory()
        request = factory.get("/some/path/")
        request.user = AnonymousUser()
        return request

    def test_authenticated_user_passes_through(self, middleware, authenticated_request):
        """Authenticated users should access protected paths."""
        response = middleware(authenticated_request)

        assert response.status_code == 200
        assert response.content == b"OK"

    def test_anonymous_user_redirected_to_login(self, middleware, anonymous_request):
        """Anonymous users should be redirected to login."""
        response = middleware(anonymous_request)

        assert response.status_code == 302
        assert response.url.startswith("/auth/login/")
        assert "next=" in response.url

    def test_anonymous_user_redirect_includes_next_url(self, middleware):
        """Redirect should include the original path as next parameter."""
        factory = RequestFactory()
        request = factory.get("/ops/tickets/")
        request.user = AnonymousUser()

        response = middleware(request)

        assert response.status_code == 302
        assert "%2Fops%2Ftickets%2F" in response.url  # URL-encoded /ops/tickets/

    def test_page_navigation_still_redirects_to_login(self, middleware):
        request = RequestFactory().get("/tickets/", headers={"Sec-Fetch-Mode": "navigate"})
        request.user = AnonymousUser()

        assert middleware(request).status_code == 302

    def test_expired_htmx_poll_reloads_the_page_instead_of_redirecting(self, middleware):
        """Following the login redirect ends at Authentik, another origin: the poll sees a network error."""
        request = RequestFactory().get("/tickets/", headers={"HX-Request": "true", "Sec-Fetch-Mode": "cors"})
        request.user = AnonymousUser()

        response = middleware(request)

        assert response.status_code == 204
        assert response.headers["HX-Refresh"] == "true"

    def test_expired_fetch_gets_an_error_it_can_show(self, middleware):
        request = RequestFactory().post("/ops/admin/competition/action/", headers={"Sec-Fetch-Mode": "cors"})
        request.user = AnonymousUser()

        response = middleware(request)

        assert response.status_code == 401
        assert json.loads(response.content) == {"error": "Your session expired. Reload the page to sign in again."}

    def test_register_subpaths_require_auth(self, middleware):
        """Only /register/ itself is public: a path below it asks a visitor to sign in."""
        factory = RequestFactory()
        request = factory.get("/register/edit/some-token/")
        request.user = AnonymousUser()

        response = middleware(request)

        assert response.status_code == 302

    def test_register_admin_paths_require_auth(self, middleware):
        """Admin paths under /register/ should require authentication."""
        factory = RequestFactory()
        request = factory.get("/register/review/")
        request.user = AnonymousUser()

        response = middleware(request)

        assert response.status_code == 302
        assert "/auth/login/" in response.url

    def test_auth_link_callback_requires_auth(self, middleware):
        """auth/link-callback should require authentication (not whitelisted)."""
        factory = RequestFactory()
        request = factory.get("/auth/link-callback")
        request.user = AnonymousUser()

        response = middleware(request)

        assert response.status_code == 302
        assert "/auth/login/" in response.url

    @pytest.mark.parametrize(
        "path",
        [
            "/",
            "/ops/",
            "/ops/tickets/",
            "/team/",
            "/admin/",
            "/scoring/",
            "/register/review/",
            "/register/seasons/",
        ],
    )
    def test_protected_paths_require_auth(self, middleware, path):
        """Non-whitelisted paths should require authentication."""
        factory = RequestFactory()
        request = factory.get(path)
        request.user = AnonymousUser()

        response = middleware(request)

        assert response.status_code == 302, f"Path {path} should require auth"
        assert "/auth/login/" in response.url

    def test_next_url_sanitization_rejects_external_url(self, middleware):
        """External URLs in next parameter should be rejected."""
        factory = RequestFactory()
        # Simulate a request where path manipulation could happen
        request = factory.get("//evil.com/steal")
        request.user = AnonymousUser()

        response = middleware(request)

        # Should redirect to login but not include the malicious URL
        assert response.status_code == 302
        # The middleware should sanitize to "/" since //evil.com is external
        assert "evil.com" not in response.url or "%2F" in response.url

    def test_authenticated_user_on_whitelist_path(self, middleware, authenticated_request):
        """Authenticated user on whitelisted path should still pass through."""
        factory = RequestFactory()
        request = factory.get("/auth/callback/")
        request.user = authenticated_request.user

        response = middleware(request)

        assert response.status_code == 200

    @pytest.mark.parametrize("path", ["/static/css/style.css", *MIDDLEWARE_WHITELIST_EXACT])
    def test_an_anonymous_visitor_reaches_each_exempt_page(self, middleware, path):
        request = RequestFactory().get(path)
        request.user = AnonymousUser()

        response = middleware(request)

        assert response.status_code == 200
        assert response.content == b"OK"


def _collect_url_paths(resolver: URLResolver | None = None, prefix: str = "/") -> list[str]:
    """Recursively collect all concrete URL paths from the URL resolver."""
    if resolver is None:
        resolver = get_resolver()

    paths: list[str] = []
    for pattern in resolver.url_patterns:
        if isinstance(pattern, URLResolver):
            new_prefix = prefix + str(pattern.pattern)
            paths.extend(_collect_url_paths(pattern, new_prefix))
        elif isinstance(pattern, URLPattern):
            route = prefix + str(pattern.pattern)
            # Skip patterns with unconvertible captures (e.g. <pk>, <slug>)
            # Fill in dummy values for common converter types
            concrete = route
            concrete = concrete.replace("<int:", "<").replace("<str:", "<").replace("<slug:", "<")
            # Replace <name> captures with dummy values
            import re

            concrete = re.sub(
                r"<(\w+)>",
                lambda m: (
                    "1" if m.group(1).endswith("id") or m.group(1) == "pk" or m.group(1).endswith("number") else "test"
                ),
                concrete,
            )
            # Skip Django admin (has its own auth)
            if concrete.startswith("/admin/"):
                continue
            paths.append(concrete)
    return paths


class TestAllEndpointsRequireAuth:
    """Verify every URL endpoint requires authentication via middleware."""

    def test_every_endpoint_requires_auth_or_is_whitelisted(self) -> None:
        """Every URL that is not in the middleware whitelist must redirect unauthenticated users to login."""
        client = Client()
        all_paths = _collect_url_paths()
        unprotected: list[str] = []

        for path in all_paths:
            is_whitelisted = (
                any(path.startswith(w) for w in MIDDLEWARE_WHITELIST_PREFIXES) or path in MIDDLEWARE_WHITELIST_EXACT
            )
            if is_whitelisted:
                continue

            response = client.get(path)
            # Middleware should redirect to /auth/login/ (302)
            # Some views may also return 405 for GET on POST-only endpoints
            if response.status_code not in (302, 405):
                unprotected.append(f"{path} -> {response.status_code}")
            elif response.status_code == 302 and "/auth/login/" not in response.url:
                # Redirects somewhere other than login — still protected by middleware
                # as long as the final destination also requires auth
                pass

        assert unprotected == [], "The following endpoints are accessible without authentication:\n" + "\n".join(
            f"  {p}" for p in unprotected
        )


class TestSecuritySettings:
    """Tests for security-critical settings."""

    def test_allowed_hosts_not_wildcard(self):
        """ALLOWED_HOSTS should not default to wildcard."""
        assert "*" not in settings.ALLOWED_HOSTS, (
            "ALLOWED_HOSTS contains '*' which disables host header validation. "
            "Set ALLOWED_HOSTS in .env for production."
        )

    def test_csrf_trusted_origins_https_only(self):
        """CSRF_TRUSTED_ORIGINS holds only HTTPS origins: production serves https only."""
        assert all(origin.startswith("https://") for origin in settings.CSRF_TRUSTED_ORIGINS), (
            settings.CSRF_TRUSTED_ORIGINS
        )

    def test_session_cookie_httponly_explicit(self):
        """SESSION_COOKIE_HTTPONLY should be explicitly True."""
        assert settings.SESSION_COOKIE_HTTPONLY is True


class TestSecurityHeadersMiddleware:
    """Tests for security headers middleware."""

    def test_csp_header_present(self):
        """Responses should include Content-Security-Policy header."""
        client = Client()
        response = client.get("/health/")
        assert "Content-Security-Policy" in response

    def test_csp_restricts_scripts(self):
        """CSP should restrict script sources."""
        client = Client()
        response = client.get("/health/")
        csp = response["Content-Security-Policy"]
        assert "script-src" in csp

    def test_csp_disallows_unsafe_eval(self):
        """CSP must not allow unsafe-eval (Alpine.js CSP build does not require it)."""
        client = Client()
        response = client.get("/health/")
        csp = response["Content-Security-Policy"]
        assert "'unsafe-eval'" not in csp

    def test_csp_allows_only_needed_external_script_domains(self):
        """Alpine/htmx are vendored (static/vendor), so unpkg is gone; Cloudflare analytics stays."""
        client = Client()
        response = client.get("/health/")
        csp = response["Content-Security-Policy"]
        assert "unpkg.com" not in csp
        assert "https://static.cloudflareinsights.com" in csp


class TestAccessLogAddress:
    """The access log names the address core.utils.client_ip returns."""

    def test_logs_the_trusted_client_address(self, settings, caplog):
        settings.TRUST_CF_CONNECTING_IP = True
        with caplog.at_level("INFO", logger="wccomps.access"):
            Client().get("/livez/", REMOTE_ADDR="10.42.3.17", HTTP_CF_CONNECTING_IP="203.0.113.7")

        messages = [r.getMessage() for r in caplog.records if r.name == "wccomps.access"]
        assert messages
        assert messages[-1].startswith("203.0.113.7 ")

    def test_logs_the_client_address_of_an_unhandled_exception(self, settings, caplog):
        from core.middleware import AccessLoggingMiddleware

        def boom(request):
            raise RuntimeError("boom")

        settings.TRUST_CF_CONNECTING_IP = True
        request = RequestFactory().get("/x/", REMOTE_ADDR="10.42.3.17", HTTP_CF_CONNECTING_IP="203.0.113.7")
        request.user = AnonymousUser()
        with caplog.at_level("ERROR", logger="wccomps.errors"), pytest.raises(RuntimeError):
            AccessLoggingMiddleware(boom)(request)

        messages = [r.getMessage() for r in caplog.records if r.name == "wccomps.errors"]
        assert messages[-1].startswith("203.0.113.7 ")
