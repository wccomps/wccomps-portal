import os
import secrets
from pathlib import Path

import django_stubs_ext

django_stubs_ext.monkeypatch()

BASE_DIR = Path(__file__).resolve().parent.parent


SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", secrets.token_urlsafe(50))

DEBUG = os.environ.get("DJANGO_DEBUG", "False") == "True"

ALLOWED_HOSTS = os.environ.get("ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")

CSRF_TRUSTED_ORIGINS = [
    "https://bot.wccomps.org",
    "https://register.wccomps.org",
    "https://team.wccomps.org",
    "https://teams.wccomps.org",
    "https://ticket.wccomps.org",
    "https://tickets.wccomps.org",
    "https://portal.wccomps.org",
]

# Requests on LEGACY_HOSTS are permanently redirected to CANONICAL_HOST (path and query preserved).
# Set CANONICAL_HOST= (empty) in the environment to disable the redirects without a code change.
# Legacy hosts must stay in ALLOWED_HOSTS and the HTTPRoute to be redirected (the redirect runs
# before the CSRF check), and in CSRF_TRUSTED_ORIGINS so their forms still work with it switched off.
CANONICAL_HOST = os.environ.get("CANONICAL_HOST", "portal.wccomps.org")
LEGACY_HOSTS = [
    "bot.wccomps.org",
    "register.wccomps.org",
    "team.wccomps.org",
    "teams.wccomps.org",
    "ticket.wccomps.org",
    "tickets.wccomps.org",
]


INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django_cotton",
    "core",
    "team",
    "ticketing",
    "quotient",
    "scoring",
    "orange_team",
    "packets",
    "registration",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "core.middleware.SecurityHeadersMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "core.middleware.SubdomainRedirectMiddleware",
    "core.middleware.AuthentikRequiredMiddleware",
    "core.middleware.UserTimezoneMiddleware",
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
            ],
        },
    },
]

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
            "options": "-c statement_timeout=30000",
        },
    }
}


AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]


LANGUAGE_CODE = "en-us"

TIME_ZONE = "UTC"

USE_I18N = True

USE_TZ = True


STATIC_URL = "static/"
STATIC_ROOT = str(BASE_DIR / "staticfiles")
STATICFILES_DIRS = [BASE_DIR / "static"]

MEDIA_URL = "/media/"
MEDIA_ROOT = str(BASE_DIR / "media")

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

LOGIN_URL = "/auth/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/"

AUTHENTIK_URL = os.environ.get("AUTHENTIK_URL", "https://auth.wccomps.org")
AUTHENTIK_CLIENT_ID = os.environ.get("AUTHENTIK_CLIENT_ID")
AUTHENTIK_SECRET = os.environ.get("AUTHENTIK_SECRET")
AUTHENTIK_OIDC_URL = os.environ.get(
    "AUTHENTIK_OIDC_URL",
    f"{AUTHENTIK_URL}/application/o/discord-bot/",
)

# Session configuration (12-hour expiry for competitions)
SESSION_COOKIE_AGE = 43200
SESSION_SAVE_EVERY_REQUEST = True
SESSION_COOKIE_SAMESITE = "Lax"  # Allow cookies across OAuth redirects
SESSION_COOKIE_HTTPONLY = True  # Explicit (Django default, but important for security audits)

BASE_URL = os.environ.get("BASE_URL", "http://localhost:5000")
DISCORD_LOG_CHANNEL_ID = int(os.environ.get("DISCORD_LOG_CHANNEL_ID", "0"))
DISCORD_TICKET_QUEUE_CHANNEL_ID = int(os.environ.get("DISCORD_TICKET_QUEUE_CHANNEL_ID", "0"))
DISCORD_ANNOUNCEMENT_CHANNEL_ID = int(os.environ.get("DISCORD_ANNOUNCEMENT_CHANNEL_ID", "0"))
# Channel where combined link+ticket panel is posted (#welcome-rules)
DISCORD_WELCOME_CHANNEL_ID = int(os.environ.get("DISCORD_WELCOME_CHANNEL_ID", "0"))
# Channel where link-only panel is posted (#link - hidden after linking)
DISCORD_LINK_CHANNEL_ID = int(os.environ.get("DISCORD_LINK_CHANNEL_ID", "0"))
BLUETEAM_ROLE_ID = int(os.environ.get("BLUETEAM_ROLE_ID", "0"))

BLACKTEAM_ROLE_ID = int(os.environ.get("BLACKTEAM_ROLE_ID", "0"))
WHITETEAM_ROLE_ID = int(os.environ.get("WHITETEAM_ROLE_ID", "0"))
ORANGETEAM_ROLE_ID = int(os.environ.get("ORANGETEAM_ROLE_ID", "0"))
REDTEAM_ROLE_ID = int(os.environ.get("REDTEAM_ROLE_ID", "0"))
GOLDTEAM_ROLE_ID = int(os.environ.get("GOLDTEAM_ROLE_ID", "0"))

GROUP_ROLE_MAPPING = {
    "WCComps_BlackTeam": BLACKTEAM_ROLE_ID,
    "WCComps_WhiteTeam": WHITETEAM_ROLE_ID,
    "WCComps_OrangeTeam": ORANGETEAM_ROLE_ID,
    "WCComps_RedTeam": REDTEAM_ROLE_ID,
    "WCComps_GoldTeam": GOLDTEAM_ROLE_ID,
}

# Staff link their accounts from the volunteer guild (/link only); roles live in the competition guild
VOLUNTEER_GUILD_ID = int(os.environ.get("VOLUNTEER_GUILD_ID", "0"))
COMPETITION_GUILD_ID = int(os.environ.get("DISCORD_GUILD_ID", "0"))

AUTHENTIK_TOKEN = os.environ.get("AUTHENTIK_TOKEN", "")

QUOTIENT_API_URL = os.environ.get("QUOTIENT_API_URL", "https://scoring.wccomps.org")

# Quotient login: an [[admin]] account from Quotient's event.conf
QUOTIENT_USERNAME = os.environ.get("QUOTIENT_USERNAME", "")
QUOTIENT_PASSWORD = os.environ.get("QUOTIENT_PASSWORD", "")

# Email configuration for packet distribution
EMAIL_BACKEND = os.environ.get("EMAIL_BACKEND", "django.core.mail.backends.smtp.EmailBackend")
EMAIL_HOST = os.environ.get("EMAIL_HOST", "localhost")
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "587"))
EMAIL_USE_TLS = os.environ.get("EMAIL_USE_TLS", "True") == "True"
EMAIL_USE_SSL = os.environ.get("EMAIL_USE_SSL", "False") == "True"
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "noreply@wccomps.org")
DEFAULT_REPLY_TO_EMAIL = os.environ.get("DEFAULT_REPLY_TO_EMAIL", "info@wccomps.org")
SERVER_EMAIL = os.environ.get("SERVER_EMAIL", DEFAULT_FROM_EMAIL)

HTTPX_DEFAULT_TIMEOUT = int(os.environ.get("HTTPX_DEFAULT_TIMEOUT", "10"))
DISCORD_WEBHOOK_TIMEOUT = int(os.environ.get("DISCORD_WEBHOOK_TIMEOUT", "5"))

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
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
