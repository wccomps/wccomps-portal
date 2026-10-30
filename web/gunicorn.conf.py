"""Gunicorn settings, loaded automatically from the working directory (/app/web)."""

import os

from gunicorn.glogging import Logger, SafeAtoms

from core.log_safety import LineSafeFormatter, escape_log_text

bind = "0.0.0.0:8000"
# Default 4. Each keeps one database connection open (CONN_MAX_AGE); mind Postgres max_connections
workers = int(os.environ.get("GUNICORN_WORKERS", "4"))
# Sync workers are killed after this long on one request. Streamed operations run in their own thread
# (core.utils.run_detached), so they finish even if the browser leaves; with it attached, this caps the request.
timeout = 300
# Load Django once in the master, before binding and forking, instead of in every worker on its first request:
# a new pod's cold workers took seconds per request (and CPU from each other) while already in service.
# portal.wsgi also warms the master (portal.warmup) at that point.
preload_app = True
accesslog = "-"
errorlog = "-"
loglevel = "info"

# Default format minus the query string, referer and Basic auth user: /auth/link?token=... and
# /auth/callback/?code=... would otherwise put link tokens and OAuth codes in the logs.
# %(h)s is the connecting peer (the proxy in production); the app's own access log names the client when
# TRUST_CF_CONNECTING_IP is on
access_log_format = '%(h)s %(l)s %(t)s "%(m)s %(U)s %(H)s" %(s)s %(b)s "%(a)s"'


class EscapingAtoms(SafeAtoms):
    """SafeAtoms (header lookup, "-" for a missing atom) escaping with escape_log_text instead of quotes alone."""

    def __init__(self, atoms: dict[str, object]) -> None:
        dict.__init__(self, {key: escape_log_text(v) if isinstance(v, str) else v for key, v in atoms.items()})


class EscapingLogger(Logger):
    """Escapes request text in the access line, and keeps each error log message (a raw request URI) on one line."""

    atoms_wrapper_class = EscapingAtoms

    def _set_handler(self, log, output, fmt, stream=None):
        super()._set_handler(log, output, LineSafeFormatter(fmt._fmt, fmt.datefmt), stream)


logger_class = EscapingLogger
