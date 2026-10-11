import os

from django.core.exceptions import ImproperlyConfigured
from django.core.wsgi import get_wsgi_application

# settings.py falls back to a random key so tools and tests run without one. A server must not: each
# web process would sign sessions, CSRF tokens and OAuth state with its own key, and every restart
# would invalidate them. The placeholder from .env.example is public, so it counts as unset.
if os.environ.get("DJANGO_SECRET_KEY", "") in ("", "your_random_secret_key_here"):
    raise ImproperlyConfigured("DJANGO_SECRET_KEY is not set; the web server needs a fixed secret key")

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "portal.settings")

application = get_wsgi_application()
