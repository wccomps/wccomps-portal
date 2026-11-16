"""Settings for the test suite: production settings plus what tests need to run fast and in parallel."""

import os

# The suite runs as WRCCDC's deployment (its production configmap's values): fixtures use its group names, names and
# hosts. setdefault, so a test run can still override one from the environment.
for _name, _value in {
    "PORTAL_GROUP_PREFIX": "WCComps_",
    "PORTAL_ADMIN_GROUP": "WCComps_Discord_Admin",
    "ORG_NAME": "Western Cyber Competitions",
    "ORG_SHORT_NAME": "WCComps",
    "AUTHENTIK_URL": "https://auth.wccomps.org",
    "QUOTIENT_API_URL": "https://scoring.wccomps.org",
    "DEFAULT_FROM_EMAIL": "noreply@wccomps.org",
    "DEFAULT_REPLY_TO_EMAIL": "info@wccomps.org",
}.items():
    os.environ.setdefault(_name, _value)

from portal.settings import *  # noqa: F403
from portal.settings import DATABASES, LOGGING, STORAGES

# A component attribute cotton would drop fails the test that renders it (production logs an error)
STRICT_COMPONENT_ATTRS = True

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

# Close DB connections after each request so parallel xdist workers with threaded live servers
# don't exhaust connections
DATABASES["default"]["CONN_MAX_AGE"] = 0

# Plain static storage needs no collectstatic manifest, and WhiteNoise skips scanning STATIC_ROOT on every Client()
STORAGES["staticfiles"] = {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}
WHITENOISE_AUTOREFRESH = True

# pytest's caplog captures log records directly; the console StreamHandler only duplicates tracebacks to stderr
LOGGING["handlers"]["console"] = {"class": "logging.NullHandler"}
