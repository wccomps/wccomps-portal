"""Gunicorn settings, loaded automatically from the working directory (/app/web)."""

import os

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

# Default format minus the query string and referer: /auth/link?token=... and
# /auth/callback/?code=... would otherwise put link tokens and OAuth codes in the logs.
# %(h)s is the connecting peer (the proxy in production); the app's own access log names the client when
# TRUST_CF_CONNECTING_IP is on
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(m)s %(U)s %(H)s" %(s)s %(b)s "%(a)s"'
