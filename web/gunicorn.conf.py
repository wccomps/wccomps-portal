"""Gunicorn settings, loaded automatically from the working directory (/app/web)."""

import logging
import os

bind = "0.0.0.0:8000"
# Default 4. Each keeps one database connection open (CONN_MAX_AGE); mind Postgres max_connections
workers = int(os.environ.get("GUNICORN_WORKERS", "4"))
# Sync workers are killed after this long on one request. Streamed operations run in their own thread
# (core.utils.run_detached), so they finish even if the browser leaves; with it attached, this caps the request.
timeout = 300
# Load Django once in the master, before forking, instead of in every worker on its first request:
# a new pod's cold workers took seconds per request (and CPU from each other) while already in service.
preload_app = True
accesslog = "-"
errorlog = "-"
loglevel = "info"

# Default format minus the query string and referer: /auth/link?token=... and
# /auth/callback/?code=... would otherwise put link tokens and OAuth codes in the logs.
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(m)s %(U)s %(H)s" %(s)s %(b)s "%(a)s"'


def when_ready(_server: object) -> None:
    """Load in the master what a worker's first page would otherwise load, so forked workers start warm: the URLconf
    (and so every view module), its reverse lookups, the date formats and every template compiled. Cold, a worker's
    first page took about ten times a warm render."""
    from importlib import import_module
    from pathlib import Path

    from django.conf import settings
    from django.template import engines
    from django.template.utils import get_app_template_dirs
    from django.urls import get_resolver
    from django.utils.formats import get_format

    import_module(settings.ROOT_URLCONF)
    get_resolver().reverse_dict  # noqa: B018 - reading it builds it
    get_format("DATE_FORMAT")
    engine = engines["django"]
    for directory in (*engine.engine.dirs, *get_app_template_dirs("templates")):
        for path in Path(directory).rglob("*.html"):
            name = path.relative_to(directory).as_posix()
            # A template that fails to compile breaks only its own page, as it would without warming; the pod starts
            try:
                engine.get_template(name)
            except Exception:
                logging.getLogger("gunicorn.error").exception("Could not warm template %s", name)
