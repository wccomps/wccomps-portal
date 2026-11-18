import os
import secrets
from pathlib import Path
from urllib.parse import urlsplit

import django_stubs_ext
from django.core.exceptions import ImproperlyConfigured

django_stubs_ext.monkeypatch()

BASE_DIR = Path(__file__).resolve().parent.parent

_TRUE, _FALSE = {"1", "true", "yes", "on"}, {"0", "false", "no", "off"}


def env_bool(name: str, default: bool) -> bool:
    """A yes/no environment variable: 1, true, yes or on, or 0, false, no or off, in any case; unset or empty is the
    default. Anything else is refused, so a typo can't quietly turn off a check such as certificate verification."""
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return default
    if value in _TRUE | _FALSE:
        return value in _TRUE
    raise ImproperlyConfigured(f"{name} must be true or false (or 1/0, yes/no, on/off), not {value!r}")


# The random fallback lets tools and tests run without a key; portal.required stops the servers without a strong one
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", secrets.token_urlsafe(50))

DEBUG = env_bool("DJANGO_DEBUG", False)

# A component attribute that cotton would drop (core.cotton) fails the render when True, else logs an error
STRICT_COMPONENT_ATTRS = DEBUG

ALLOWED_HOSTS = os.environ.get("ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")

TRUST_CF_CONNECTING_IP = env_bool("TRUST_CF_CONNECTING_IP", False)

BASE_URL = os.environ.get("BASE_URL", "http://localhost:5000")

# The https origin of the portal's address. An http BASE_URL (local development) adds nothing: production serves
# https only, and a same-host form needs no trusted origin.
_address = urlsplit(BASE_URL)
CSRF_TRUSTED_ORIGINS = [f"https://{_address.netloc}"] if _address.scheme == "https" and _address.netloc else []

# Form fields other than files (a comment, a pasted CSV) in one request: a larger body is refused (400) before a view
# reads it. Files are capped by the views (core.stored_files) and the proxy's body size limit.
DATA_UPLOAD_MAX_MEMORY_SIZE = 256 * 1024


INSTALLED_APPS = [
    "core.apps.PortalAdminConfig",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Django's widget templates, for the project template engine core.form_renderer renders with
    "django.forms",
    "django_cotton",
    "core.apps.CoreConfig",
    "team",
    "ticketing",
    "quotient",
    "scoring",
    "orange_team",
    "packets",
    "registration",
]

# A failed CSRF check is answered as every other refusal is (the error page, or JSON for a fetch)
CSRF_FAILURE_VIEW = "core.errors.csrf_failure"

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "core.middleware.SecurityHeadersMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "core.middleware.RejectNulMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "core.middleware.AuthentikRequiredMiddleware",
    "core.middleware.RejectNulFieldsMiddleware",
    "core.middleware.UserTimezoneMiddleware",
    "core.errors.MethodNotAllowedMiddleware",
    "core.middleware.AccessLoggingMiddleware",
]

ROOT_URLCONF = "portal.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "core.context_processors.permissions",
            ],
            "builtins": [
                "django_cotton.templatetags.cotton",
                "core.templatetags.js_filters",
                "core.templatetags.text_filters",
            ],
        },
    },
]

FORM_RENDERER = "core.form_renderer.PortalFormRenderer"

WSGI_APPLICATION = "portal.wsgi.application"


DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("DB_NAME", "wccomps"),
        "USER": os.environ.get("DB_USER", "wccomps"),
        "PASSWORD": os.environ.get("DB_PASSWORD", "wccomps"),
        "HOST": os.environ.get("DB_HOST", "localhost"),
        "PORT": os.environ.get("DB_PORT", "5432"),
        "CONN_MAX_AGE": 600,
        "CONN_HEALTH_CHECKS": True,
        "OPTIONS": {
            "connect_timeout": 10,
            # Keepalives make Postgres drop a half-open connection, and any session lock it holds, within minutes
            "options": (
                "-c statement_timeout=30000 -c tcp_keepalives_idle=60 -c tcp_keepalives_interval=10 "
                "-c tcp_keepalives_count=6"
            ),
        },
    }
}


LANGUAGE_CODE = "en-us"

TIME_ZONE = "UTC"

USE_I18N = True

USE_TZ = True


STATIC_URL = "static/"
STATIC_ROOT = str(BASE_DIR / "staticfiles")
STATICFILES_DIRS = [BASE_DIR / "static"]

STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

# Trust X-Forwarded-Proto header from the reverse proxy (Cloudflare tunnel, then the gateway)
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

if not DEBUG:
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    # Don't redirect to HTTPS: Cloudflare terminates it
    SECURE_SSL_REDIRECT = False
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Sessions, and Django's model permissions from the admin permission; no password sign-in (core.auth_backends)
AUTHENTICATION_BACKENDS = ["core.auth_backends.PortalBackend"]

LOGIN_URL = "/auth/login/"

AUTHENTIK_URL = os.environ.get("AUTHENTIK_URL", "")
AUTHENTIK_CLIENT_ID = os.environ.get("AUTHENTIK_CLIENT_ID")
AUTHENTIK_SECRET = os.environ.get("AUTHENTIK_SECRET")
AUTHENTIK_OIDC_URL = os.environ.get(
    "AUTHENTIK_OIDC_URL",
    f"{AUTHENTIK_URL}/application/o/discord-bot/" if AUTHENTIK_URL else "",
)

# Authentik group names (read by core.permission_constants): each role's group, by default this prefix plus a fixed
# suffix; a name may contain spaces
PORTAL_GROUP_PREFIX = os.environ.get("PORTAL_GROUP_PREFIX", "Portal_")
PORTAL_ADMIN_GROUP = os.environ.get("PORTAL_ADMIN_GROUP", f"{PORTAL_GROUP_PREFIX}Admin")
# A role setting that is unset or empty takes its default
PORTAL_GOLD_GROUP = os.environ.get("PORTAL_GOLD_GROUP") or f"{PORTAL_GROUP_PREFIX}GoldTeam"
PORTAL_WHITE_GROUP = os.environ.get("PORTAL_WHITE_GROUP") or f"{PORTAL_GROUP_PREFIX}WhiteTeam"
PORTAL_WHITE_LEAD_GROUP = os.environ.get("PORTAL_WHITE_LEAD_GROUP") or f"{PORTAL_GROUP_PREFIX}WhiteTeam_Lead"
PORTAL_ORANGE_GROUP = os.environ.get("PORTAL_ORANGE_GROUP") or f"{PORTAL_GROUP_PREFIX}OrangeTeam"
PORTAL_ORANGE_LEAD_GROUP = os.environ.get("PORTAL_ORANGE_LEAD_GROUP") or f"{PORTAL_GROUP_PREFIX}OrangeTeam_Lead"
PORTAL_RED_GROUP = os.environ.get("PORTAL_RED_GROUP") or f"{PORTAL_GROUP_PREFIX}RedTeam"
# Grants no permission: only its Discord role (GROUP_ROLE_MAPPING)
PORTAL_BLACK_GROUP = os.environ.get("PORTAL_BLACK_GROUP") or f"{PORTAL_GROUP_PREFIX}BlackTeam"
PORTAL_TICKETING_ADMIN_GROUP = os.environ.get("PORTAL_TICKETING_ADMIN_GROUP") or f"{PORTAL_GROUP_PREFIX}Ticketing_Admin"
PORTAL_TICKETING_SUPPORT_GROUP = (
    os.environ.get("PORTAL_TICKETING_SUPPORT_GROUP") or f"{PORTAL_GROUP_PREFIX}Ticketing_Support"
)
# Two roles sharing a group would grant each other's permissions, and GROUP_ROLE_MAPPING would keep only one's role
_role_setting_of: dict[str, str] = {}
for _setting, _group in {
    "PORTAL_ADMIN_GROUP": PORTAL_ADMIN_GROUP,
    "PORTAL_GOLD_GROUP": PORTAL_GOLD_GROUP,
    "PORTAL_WHITE_GROUP": PORTAL_WHITE_GROUP,
    "PORTAL_WHITE_LEAD_GROUP": PORTAL_WHITE_LEAD_GROUP,
    "PORTAL_ORANGE_GROUP": PORTAL_ORANGE_GROUP,
    "PORTAL_ORANGE_LEAD_GROUP": PORTAL_ORANGE_LEAD_GROUP,
    "PORTAL_RED_GROUP": PORTAL_RED_GROUP,
    "PORTAL_BLACK_GROUP": PORTAL_BLACK_GROUP,
    "PORTAL_TICKETING_ADMIN_GROUP": PORTAL_TICKETING_ADMIN_GROUP,
    "PORTAL_TICKETING_SUPPORT_GROUP": PORTAL_TICKETING_SUPPORT_GROUP,
}.items():
    if _group in _role_setting_of:
        raise ImproperlyConfigured(
            f"{_role_setting_of[_group]} and {_setting} both name the Authentik group {_group!r}; give each its own"
        )
    _role_setting_of[_group] = _setting
# A new team's default Authentik group, a str.format pattern given the team's number
TEAM_GROUP_PATTERN = os.environ.get("TEAM_GROUP_PATTERN", f"{PORTAL_GROUP_PREFIX}BlueTeam{{number:02d}}")
# A team's accounts, with the literal text {number} standing for the team's two-digit number: a pattern with no regular
# expression operator names the team's one account, by exact username; otherwise the team's accounts are the members
# of its group whose username fully matches this regular expression ("team{number}[a-i]" gives each team nine)
TEAM_ACCOUNT_PATTERN = os.environ.get("TEAM_ACCOUNT_PATTERN", "team{number}")
if "{number}" not in TEAM_ACCOUNT_PATTERN:
    raise ImproperlyConfigured("TEAM_ACCOUNT_PATTERN must contain {number}, or every team shares the same accounts")
# Team folder mode: the Authentik user path holding team accounts when there are no team groups; empty uses team groups
TEAM_ACCOUNT_PATH = os.environ.get("TEAM_ACCOUNT_PATH", "")
try:
    _distinct_groups = TEAM_GROUP_PATTERN.format(number=1) != TEAM_GROUP_PATTERN.format(number=2)
except KeyError, IndexError, ValueError:
    _distinct_groups = False
if not _distinct_groups:
    raise ImproperlyConfigured(
        "TEAM_GROUP_PATTERN must be a str.format pattern using {number}, such as Team{number:02d}"
    )
MAX_TEAMS = int(os.environ.get("MAX_TEAMS", "50"))

# Session configuration (12-hour expiry for competitions)
SESSION_COOKIE_AGE = 43200
SESSION_SAVE_EVERY_REQUEST = True
SESSION_COOKIE_SAMESITE = "Lax"  # Allow cookies across OAuth redirects
SESSION_COOKIE_HTTPONLY = True  # Explicit (Django default, but important for security audits)

# The Discord bot's token; without one the portal queues no Discord work (core.task_backends)
DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
# Zulip bot credentials; the portal queues Zulip notices only when all three are set
ZULIP_SITE = os.environ.get("ZULIP_SITE", "")
ZULIP_BOT_EMAIL = os.environ.get("ZULIP_BOT_EMAIL", "")
ZULIP_API_KEY = os.environ.get("ZULIP_API_KEY", "")
# Where Zulip notices go: each team's stream (a str.format pattern using {number}), the staff ticket stream (empty: no
# separate staff alert, the new ticket's post in its team's stream is the only one), the announcements stream, and the
# stream and topic for ops log lines
ZULIP_TEAM_STREAM = os.environ.get("ZULIP_TEAM_STREAM", "team{number:02d}")
# A broadcast to teams: each team's stream (a pattern using {number}; empty: ZULIP_TEAM_STREAM) and topic (may use
# {number}). Tickets stay in ZULIP_TEAM_STREAM.
ZULIP_TEAM_BROADCAST_STREAM = os.environ.get("ZULIP_TEAM_BROADCAST_STREAM", "")
ZULIP_TEAM_BROADCAST_TOPIC = os.environ.get("ZULIP_TEAM_BROADCAST_TOPIC", "announcements")
for _setting, _pattern in (
    ("ZULIP_TEAM_BROADCAST_STREAM", ZULIP_TEAM_BROADCAST_STREAM),
    ("ZULIP_TEAM_BROADCAST_TOPIC", ZULIP_TEAM_BROADCAST_TOPIC),
):
    try:
        _pattern.format(number=1)
    except KeyError, IndexError, ValueError, TypeError, AttributeError:
        raise ImproperlyConfigured(
            f"{_setting} must be a str.format pattern given the team's number, such as Team {{number:02d}} Chat"
        ) from None
# One stream for every team would show each team's broadcasts to all of them
_one_stream = ZULIP_TEAM_BROADCAST_STREAM.format(number=1) == ZULIP_TEAM_BROADCAST_STREAM.format(number=2)
if ZULIP_TEAM_BROADCAST_STREAM and _one_stream:
    raise ImproperlyConfigured("ZULIP_TEAM_BROADCAST_STREAM must use {number}, giving each team its own stream")
ZULIP_STAFF_STREAM = os.environ.get("ZULIP_STAFF_STREAM", "tickets")
ZULIP_ANNOUNCEMENTS_STREAM = os.environ.get("ZULIP_ANNOUNCEMENTS_STREAM", "announcements")
ZULIP_OPS_STREAM = os.environ.get("ZULIP_OPS_STREAM", "ops")
ZULIP_OPS_TOPIC = os.environ.get("ZULIP_OPS_TOPIC", "ops")
# User groups a notice mentions: a team's (a pattern using {number}, such as "Team {number:02d}") and staff's; empty
# mentions no one
ZULIP_TEAM_MENTION = os.environ.get("ZULIP_TEAM_MENTION", "")
ZULIP_STAFF_MENTION = os.environ.get("ZULIP_STAFF_MENTION", "")

DISCORD_LOG_CHANNEL_ID = int(os.environ.get("DISCORD_LOG_CHANNEL_ID", "0"))
DISCORD_TICKET_QUEUE_CHANNEL_ID = int(os.environ.get("DISCORD_TICKET_QUEUE_CHANNEL_ID", "0"))
DISCORD_ANNOUNCEMENT_CHANNEL_ID = int(os.environ.get("DISCORD_ANNOUNCEMENT_CHANNEL_ID", "0"))
# Channel where ticket panel is posted (#welcome-rules)
DISCORD_WELCOME_CHANNEL_ID = int(os.environ.get("DISCORD_WELCOME_CHANNEL_ID", "0"))
# Channel where link-only panel is posted (#link - hidden after linking)
DISCORD_LINK_CHANNEL_ID = int(os.environ.get("DISCORD_LINK_CHANNEL_ID", "0"))
# Name of a competition guild role that may talk in every team category beside the guest roles; empty: none
DISCORD_SERVER_OWNER_ROLE = os.environ.get("DISCORD_SERVER_OWNER_ROLE", "")
BLUETEAM_ROLE_ID = int(os.environ.get("BLUETEAM_ROLE_ID", "0"))

BLACKTEAM_ROLE_ID = int(os.environ.get("BLACKTEAM_ROLE_ID", "0"))
WHITETEAM_ROLE_ID = int(os.environ.get("WHITETEAM_ROLE_ID", "0"))
ORANGETEAM_ROLE_ID = int(os.environ.get("ORANGETEAM_ROLE_ID", "0"))
REDTEAM_ROLE_ID = int(os.environ.get("REDTEAM_ROLE_ID", "0"))
GOLDTEAM_ROLE_ID = int(os.environ.get("GOLDTEAM_ROLE_ID", "0"))

GROUP_ROLE_MAPPING = {
    PORTAL_BLACK_GROUP: BLACKTEAM_ROLE_ID,
    PORTAL_WHITE_GROUP: WHITETEAM_ROLE_ID,
    PORTAL_ORANGE_GROUP: ORANGETEAM_ROLE_ID,
    PORTAL_RED_GROUP: REDTEAM_ROLE_ID,
    PORTAL_GOLD_GROUP: GOLDTEAM_ROLE_ID,
}

# Staff link their accounts from the volunteer guild (/link only); roles live in the competition guild
VOLUNTEER_GUILD_ID = int(os.environ.get("VOLUNTEER_GUILD_ID", "0"))
COMPETITION_GUILD_ID = int(os.environ.get("DISCORD_GUILD_ID", "0"))

AUTHENTIK_TOKEN = os.environ.get("AUTHENTIK_TOKEN", "")

QUOTIENT_API_URL = os.environ.get("QUOTIENT_API_URL", "")

# Quotient login: an [[admin]] account from Quotient's event.conf
QUOTIENT_USERNAME = os.environ.get("QUOTIENT_USERNAME", "")
QUOTIENT_PASSWORD = os.environ.get("QUOTIENT_PASSWORD", "")

PROXMOX_API_URL = os.environ.get("PROXMOX_API_URL", "")
PROXMOX_API_TOKEN = os.environ.get("PROXMOX_API_TOKEN", "")
PROXMOX_CA_FILE = os.environ.get("PROXMOX_CA_FILE", "")
PROXMOX_VERIFY_SSL = env_bool("PROXMOX_VERIFY_SSL", True)
PROXMOX_DEFAULT_SNAPSHOT = os.environ.get("PROXMOX_DEFAULT_SNAPSHOT", "clean")
# The ticket category whose tickets get the Proxmox box reset (seeded in ticketing 0009 as "Box Reset / Scrub")
BOX_RESET_CATEGORY_ID = int(os.environ.get("BOX_RESET_CATEGORY_ID", "2"))

AWEFUL_PROXY_URL = os.environ.get("AWEFUL_PROXY_URL", "")
AWEFUL_PROXY_TOKEN = os.environ.get("AWEFUL_PROXY_TOKEN", "")

# Settings among portal.required.HTTPS_SETTINGS (comma-separated) whose URL may be plain http: only for a service on a
# network nobody else can read, such as an in-cluster address. Empty: every credentialed URL must be https.
ALLOW_HTTP_URLS = [name.strip() for name in os.environ.get("ALLOW_HTTP_URLS", "").split(",") if name.strip()]

NEXTCLOUD_URL = os.environ.get("NEXTCLOUD_URL", "")
NEXTCLOUD_USERNAME = os.environ.get("NEXTCLOUD_USERNAME", "")
NEXTCLOUD_PASSWORD = os.environ.get("NEXTCLOUD_PASSWORD", "")
NEXTCLOUD_OIDC_PROVIDER_ID = int(os.environ.get("NEXTCLOUD_OIDC_PROVIDER_ID", "1"))

# Email configuration for packet and scorecard distribution
_EMAIL_BACKEND = os.environ.get("EMAIL_BACKEND", "django.core.mail.backends.smtp.EmailBackend")
MAILERS = {
    "default": {
        "BACKEND": _EMAIL_BACKEND,
        **(
            {
                "OPTIONS": {
                    "host": os.environ.get("EMAIL_HOST", "localhost"),
                    "port": int(os.environ.get("EMAIL_PORT", "587")),
                    "use_tls": env_bool("EMAIL_USE_TLS", True),
                    "use_ssl": env_bool("EMAIL_USE_SSL", False),
                    "username": os.environ.get("EMAIL_HOST_USER", ""),
                    "password": os.environ.get("EMAIL_HOST_PASSWORD", ""),
                }
            }
            if _EMAIL_BACKEND == "django.core.mail.backends.smtp.EmailBackend"
            else {}
        ),
    }
}
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "")
# Packet emails carry this Reply-To when it is set
DEFAULT_REPLY_TO_EMAIL = os.environ.get("DEFAULT_REPLY_TO_EMAIL", "")
SERVER_EMAIL = os.environ.get("SERVER_EMAIL", DEFAULT_FROM_EMAIL or "root@localhost")

# AUTHENTIK_URL, DEFAULT_FROM_EMAIL, ORG_NAME and ORG_SHORT_NAME are required to serve (portal.required)
ORG_NAME = os.environ.get("ORG_NAME", "")
ORG_SHORT_NAME = os.environ.get("ORG_SHORT_NAME", "")
SITE_NAME = os.environ.get("SITE_NAME", f"{ORG_SHORT_NAME} Portal")

HTTPX_DEFAULT_TIMEOUT = int(os.environ.get("HTTPX_DEFAULT_TIMEOUT", "10"))
DISCORD_WEBHOOK_TIMEOUT = int(os.environ.get("DISCORD_WEBHOOK_TIMEOUT", "5"))

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "class": "core.log_safety.LineSafeFormatter",
            "format": "{levelname} {asctime} {module} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
        "discord": {
            "class": "core.discord_logging.DiscordWebhookHandler",
            "level": "ERROR",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "INFO",
    },
    "loggers": {
        "django": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "django.request": {
            "handlers": ["console", "discord"],
            "level": "ERROR",
            "propagate": False,
        },
        "wccomps.access": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "wccomps.errors": {
            "handlers": ["console", "discord"],
            "level": "ERROR",
            "propagate": False,
        },
        # httpx logs "HTTP Request: <method> <url>" at INFO; the URL carries the Discord
        # webhook token and OAuth codes, so keep its request lines out of the logs.
        "httpx": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        },
    },
}
