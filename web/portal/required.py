"""Settings a server needs, and settings it refuses.

Importing settings never refuses one: mypy, makemigrations and collectstatic load it with no environment. The web
server (portal.wsgi), the worker (worker.__main__) and the chat process (chat.__main__) call refuse_to_start, which
stops them on a required setting left empty, a weak secret key, or a credentialed service URL that isn't https.
"""

import os
from collections.abc import Callable

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

REQUIRED_SETTINGS = ("AUTHENTIK_URL", "DEFAULT_FROM_EMAIL", "ORG_NAME", "ORG_SHORT_NAME")

# .env.example's placeholder is public, so it counts as unset
SECRET_KEY_PLACEHOLDER = "your_random_secret_key_here"  # noqa: S105 - the public placeholder, refused as a key
# Django's security.W009 rule: shorter or plainer keys can be guessed
SECRET_KEY_MIN_LENGTH = 50
SECRET_KEY_MIN_UNIQUE_CHARACTERS = 5


def _aweful_proxy_urls() -> list[str]:
    # The proxy is only used, with its token, once the token is set
    return [settings.AWEFUL_PROXY_URL] if settings.AWEFUL_PROXY_TOKEN.strip() else []


# Each service URL setting whose requests carry a credential, and its URLs (a list setting has several); these must be
# https unless ALLOW_HTTP_URLS names them
_CREDENTIAL_URLS: tuple[tuple[str, Callable[[], list[str]]], ...] = (
    ("AUTHENTIK_URL", lambda: [settings.AUTHENTIK_URL]),  # the API token (core.authentik_manager)
    ("AUTHENTIK_OIDC_URL", lambda: [settings.AUTHENTIK_OIDC_URL]),  # the OAuth client secret and codes (core.oauth)
    ("NEXTCLOUD_URL", lambda: [settings.NEXTCLOUD_URL]),  # the admin login (core.nextcloud)
    ("QUOTIENT_API_URL", lambda: [settings.QUOTIENT_API_URL]),  # the admin login (quotient.client)
    ("PROXMOX_API_URL", lambda: settings.PROXMOX_API_URL.split(",")),  # the API token (core.proxmox)
    ("AWEFUL_PROXY_URL", _aweful_proxy_urls),  # the bearer token (scoring.quotient_sync)
)
HTTPS_SETTINGS = tuple(name for name, _ in _CREDENTIAL_URLS)


def missing_settings() -> list[str]:
    """The required settings that are empty."""
    return [name for name in REQUIRED_SETTINGS if not getattr(settings, name, "")]


def secret_key_problem() -> str | None:
    """Why DJANGO_SECRET_KEY can't sign this server's sessions, CSRF tokens and OAuth state, or None. Read from the
    environment: settings.py falls back to a random key, which differs per process and restart."""
    key = os.environ.get("DJANGO_SECRET_KEY", "")
    if key in ("", SECRET_KEY_PLACEHOLDER):
        return "DJANGO_SECRET_KEY is not set"
    if len(key) < SECRET_KEY_MIN_LENGTH or len(set(key)) < SECRET_KEY_MIN_UNIQUE_CHARACTERS:
        return (
            f"DJANGO_SECRET_KEY is too weak: use at least {SECRET_KEY_MIN_LENGTH} random characters "
            '(python -c "import secrets; print(secrets.token_urlsafe(50))")'
        )
    return None


def plain_http_settings() -> list[str]:
    """Why a credentialed service URL setting is refused: one with a URL that isn't https, unless ALLOW_HTTP_URLS
    names it; and a name ALLOW_HTTP_URLS gives that is none of them. The URL itself stays out of the message, as it
    could hold a login."""
    allowed = set(settings.ALLOW_HTTP_URLS)
    problems = [f"ALLOW_HTTP_URLS names {name}, which isn't checked" for name in sorted(allowed - set(HTTPS_SETTINGS))]
    for name, urls_of in _CREDENTIAL_URLS:
        plain = [url for url in urls_of() if url.strip() and not url.strip().lower().startswith("https://")]
        if plain and name not in allowed:
            problems.append(f"{name} must be https: requests to it carry a credential")
    return problems


def refuse_to_start(process: str) -> None:
    """Raise ImproperlyConfigured naming every reason this process (named in the message) mustn't run: a required
    setting left empty, a weak secret key, a credential that would cross the network in plain text."""
    problems: list[str] = []
    if missing := missing_settings():
        problems.append(f"{', '.join(missing)} not set")
    if key := secret_key_problem():
        problems.append(key)
    problems += plain_http_settings()
    if problems:
        raise ImproperlyConfigured(f"{'; '.join(problems)}; {process} refuses to start")
