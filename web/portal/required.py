"""Settings a server needs and settings.py leaves empty by default.

Importing settings never refuses a missing one: mypy, makemigrations and collectstatic load it with no environment.
The web server (portal.wsgi), the worker (worker.__main__) and the chat process (chat.__main__) refuse to start
without them.
"""

from django.conf import settings

REQUIRED_SETTINGS = ("AUTHENTIK_URL", "DEFAULT_FROM_EMAIL", "ORG_NAME", "ORG_SHORT_NAME")


def missing_settings() -> list[str]:
    """The required settings that are empty."""
    return [name for name in REQUIRED_SETTINGS if not getattr(settings, name, "")]
