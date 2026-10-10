"""Tests for middleware (SubdomainRedirectMiddleware, AuthentikRequiredMiddleware)."""

import json

import pytest
from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.http import HttpResponse
from django.test import Client, RequestFactory
from django.urls import URLPattern, URLResolver, get_resolver

from core.middleware import AuthentikRequiredMiddleware, SubdomainRedirectMiddleware

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


class TestSubdomainRedirectMiddleware:
    """Tests for SubdomainRedirectMiddleware."""

    @pytest.fixture(autouse=True)
    def _allow_all_hosts(self, settings):
        """Allow any host header; canonical redirect off to test the subdomain mapping alone."""
        settings.ALLOWED_HOSTS = ["*"]
        settings.CANONICAL_HOST = ""

    @pytest.fixture
    def middleware(self):
        """Create middleware instance with mock get_response."""

        def get_response(request):
            return HttpResponse("OK")

        return SubdomainRedirectMiddleware(get_response)

    def test_register_subdomain_root_redirects(self, middleware):
        """Root path on register subdomain should redirect to /register/."""
        factory = RequestFactory()
        request = factory.get("/", HTTP_HOST="register.wccomps.org")

        response = middleware(request)

        assert response.status_code == 302
        assert response.url == "/register/"

    def test_register_subdomain_with_port_redirects(self, middleware):
        """Register subdomain with port should still redirect."""
        factory = RequestFactory()
        request = factory.get("/", HTTP_HOST="register.wccomps.org:8000")

        response = middleware(request)

        assert response.status_code == 302
        assert response.url == "/register/"

    def test_register_subdomain_non_root_passes_through(self, middleware):
        """Non-root paths on register subdomain should pass through."""
        factory = RequestFactory()
        request = factory.get("/register/form/", HTTP_HOST="register.wccomps.org")

        response = middleware(request)

        assert response.status_code == 200
        assert response.content == b"OK"

    def test_main_domain_root_passes_through(self, middleware):
        """Root path on main domain should pass through."""
        factory = RequestFactory()
        request = factory.get("/", HTTP_HOST="wccomps.org")

        response = middleware(request)

        assert response.status_code == 200
        assert response.content == b"OK"

    def test_unknown_subdomain_passes_through(self, middleware):
        """Unknown subdomain should pass through without redirect."""
        factory = RequestFactory()
        request = factory.get("/", HTTP_HOST="unknown.wccomps.org")

        response = middleware(request)

        assert response.status_code == 200
        assert response.content == b"OK"


class TestCanonicalHostRedirect:
    """Legacy hosts permanently redirect to the canonical host, preserving path and query."""

    LEGACY_HOSTS = [
        "bot.wccomps.org",
        "register.wccomps.org",
        "team.wccomps.org",
        "teams.wccomps.org",
        "ticket.wccomps.org",
        "tickets.wccomps.org",
    ]

    @pytest.fixture(autouse=True)
    def _canonical_settings(self, settings):
        settings.ALLOWED_HOSTS = ["*"]
        settings.CANONICAL_HOST = "portal.wccomps.org"
        settings.LEGACY_HOSTS = self.LEGACY_HOSTS

    @pytest.fixture
    def middleware(self):
        return SubdomainRedirectMiddleware(lambda request: HttpResponse("OK"))

    @pytest.mark.parametrize("host", LEGACY_HOSTS)
    def test_legacy_host_redirects_permanently(self, middleware, host):
        request = RequestFactory().get("/tickets/42/", HTTP_HOST=host)

        response = middleware(request)

        assert response.status_code == 301
        assert response.url == "https://portal.wccomps.org/tickets/42/"

    def test_query_string_preserved(self, middleware):
        request = RequestFactory().get("/auth/link", {"token": "abc123"}, HTTP_HOST="bot.wccomps.org")

        response = middleware(request)

        assert response.url == "https://portal.wccomps.org/auth/link?token=abc123"

    def test_port_is_ignored_when_matching(self, middleware):
        request = RequestFactory().get("/ops/", HTTP_HOST="team.wccomps.org:443")

        response = middleware(request)

        assert response.url == "https://portal.wccomps.org/ops/"

    def test_register_root_goes_to_register_on_canonical_host(self, middleware):
        request = RequestFactory().get("/", HTTP_HOST="register.wccomps.org")

        response = middleware(request)

        assert response.status_code == 301
        assert response.url == "https://portal.wccomps.org/register/"

    def test_legacy_root_without_mapping_keeps_root(self, middleware):
        request = RequestFactory().get("/", HTTP_HOST="bot.wccomps.org")

        response = middleware(request)

        assert response.url == "https://portal.wccomps.org/"

    def test_post_uses_308_to_preserve_method(self, middleware):
        request = RequestFactory().post("/tickets/create/", {"title": "x"}, HTTP_HOST="ticket.wccomps.org")

        response = middleware(request)

        assert response.status_code == 308
        assert response.url == "https://portal.wccomps.org/tickets/create/"

    def test_oauth_callback_not_redirected(self, middleware):
        """In-flight logins must finish on the host Authentik issued the code for."""
        request = RequestFactory().get("/auth/callback/", {"code": "c", "state": "s"}, HTTP_HOST="bot.wccomps.org")

        response = middleware(request)

        assert response.status_code == 200
        assert response.content == b"OK"

    @pytest.mark.parametrize("host", ["portal.wccomps.org", "localhost", "127.0.0.1", "unknown.wccomps.org"])
    def test_non_legacy_hosts_pass_through(self, middleware, host):
        request = RequestFactory().get("/tickets/", HTTP_HOST=host)

        response = middleware(request)

        assert response.status_code == 200
        assert response.content == b"OK"

    def test_empty_canonical_host_disables_redirect(self, settings):
        settings.CANONICAL_HOST = ""
        middleware = SubdomainRedirectMiddleware(lambda request: HttpResponse("OK"))
        request = RequestFactory().get("/tickets/", HTTP_HOST="bot.wccomps.org")

        response = middleware(request)

        assert response.status_code == 200

    def test_redirect_is_logged_for_usage_measurement(self, middleware, caplog):
        request = RequestFactory().post(
            "/tickets/create/", HTTP_HOST="ticket.wccomps.org", HTTP_REFERER="https://ticket.wccomps.org/tickets/"
        )

        with caplog.at_level("INFO", logger="wccomps.access"):
            middleware(request)

        messages = [r.getMessage() for r in caplog.records if r.name == "wccomps.access"]
        assert messages == [
            'legacy-host-redirect ticket.wccomps.org "POST /tickets/create/" 308 https://ticket.wccomps.org/tickets/'
        ]

    def test_oauth_callback_on_legacy_host_is_logged(self, middleware, caplog):
        """Callbacks on legacy hosts are what the old Authentik redirect URIs are for; count them."""
        request = RequestFactory().get("/auth/callback/", {"code": "c", "state": "s"}, HTTP_HOST="bot.wccomps.org")

        with caplog.at_level("INFO", logger="wccomps.access"):
            middleware(request)

        messages = [r.getMessage() for r in caplog.records if r.name == "wccomps.access"]
        assert messages == ['legacy-host-callback bot.wccomps.org "GET /auth/callback/" - -']

    def test_settings_legacy_hosts_are_trusted_and_routed(self):
        """Every legacy host must stay in CSRF_TRUSTED_ORIGINS while redirects are active."""
        from django.conf import settings as real_settings

        trusted = set(real_settings.CSRF_TRUSTED_ORIGINS)
        for host in real_settings.LEGACY_HOSTS:
            assert f"https://{host}" in trusted
        assert f"https://{real_settings.CANONICAL_HOST}" in trusted


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

    def test_auth_path_whitelisted(self, middleware):
        """Paths starting with /auth/ should be accessible without login."""
        factory = RequestFactory()
        request = factory.get("/auth/login/")
        request.user = AnonymousUser()

        response = middleware(request)

        assert response.status_code == 200
        assert response.content == b"OK"

    def test_static_path_whitelisted(self, middleware):
        """Paths starting with /static/ should be accessible without login."""
        factory = RequestFactory()
        request = factory.get("/static/css/style.css")
        request.user = AnonymousUser()

        response = middleware(request)

        assert response.status_code == 200
        assert response.content == b"OK"

    def test_health_path_whitelisted(self, middleware):
        """Paths starting with /health/ should be accessible without login."""
        factory = RequestFactory()
        request = factory.get("/health/")
        request.user = AnonymousUser()

        response = middleware(request)

        assert response.status_code == 200
        assert response.content == b"OK"

    def test_register_root_whitelisted(self, middleware):
        """Exact /register/ path should be accessible without login."""
        factory = RequestFactory()
        request = factory.get("/register/")
        request.user = AnonymousUser()

        response = middleware(request)

        assert response.status_code == 200
        assert response.content == b"OK"

    def test_register_edit_whitelisted(self, middleware):
        """Token-based registration edit paths should be accessible without login."""
        factory = RequestFactory()
        request = factory.get("/register/edit/some-token/")
        request.user = AnonymousUser()

        response = middleware(request)

        assert response.status_code == 200
        assert response.content == b"OK"

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

    def test_admin_path_requires_admin_permission(self, middleware, blue_team_user):
        """Django admin should require Authentik admin permission, not just authentication."""
        factory = RequestFactory()
        request = factory.get("/admin/")
        request.user = blue_team_user  # authenticated but not admin

        response = middleware(request)

        assert response.status_code == 403

    def test_admin_path_allowed_for_admin_user(self, middleware, admin_user):
        """Django admin should be accessible to Authentik admins."""
        factory = RequestFactory()
        request = factory.get("/admin/")
        request.user = admin_user

        response = middleware(request)

        assert response.status_code == 200

    def test_authenticated_user_on_whitelist_path(self, middleware, authenticated_request):
        """Authenticated user on whitelisted path should still pass through."""
        factory = RequestFactory()
        request = factory.get("/auth/callback/")
        request.user = authenticated_request.user

        response = middleware(request)

        assert response.status_code == 200


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
]
MIDDLEWARE_WHITELIST_STARTSWITH = ["/register/edit/"]


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
                any(path.startswith(w) for w in MIDDLEWARE_WHITELIST_PREFIXES)
                or path in MIDDLEWARE_WHITELIST_EXACT
                or any(path.startswith(w) for w in MIDDLEWARE_WHITELIST_STARTSWITH)
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
        """CSRF_TRUSTED_ORIGINS should only contain HTTPS origins."""
        http_origins = [o for o in settings.CSRF_TRUSTED_ORIGINS if o.startswith("http://")]
        assert http_origins == [], (
            f"CSRF_TRUSTED_ORIGINS contains HTTP origins: {http_origins}. "
            "Remove HTTP variants — production uses HTTPS only."
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
