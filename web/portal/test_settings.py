"""Settings for the test suite: production settings plus what tests need to run fast and in parallel."""

from portal.settings import *  # noqa: F403
from portal.settings import DATABASES, STORAGES

# A component attribute cotton would drop fails the test that renders it (production logs a warning)
STRICT_COMPONENT_ATTRS = True

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

# Close DB connections after each request so parallel xdist workers with threaded live servers
# don't exhaust connections
DATABASES["default"]["CONN_MAX_AGE"] = 0

# Plain static storage needs no collectstatic manifest
STORAGES["staticfiles"] = {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}
