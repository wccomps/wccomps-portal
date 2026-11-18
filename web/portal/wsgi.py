import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "portal.settings")

application = get_wsgi_application()

from portal.required import refuse_to_start  # noqa: E402 - reads settings, which get_wsgi_application configured

# settings.py falls back to a random key so tools and tests run without one. A server must not: each web process
# would sign sessions, CSRF tokens and OAuth state with its own key, and every restart would invalidate them.
refuse_to_start("the web server")

# Gunicorn's preload_app imports this module in the master before it binds the port, so workers fork warm
from portal.warmup import warm  # noqa: E402 - needs the settings get_wsgi_application set up

warm()
