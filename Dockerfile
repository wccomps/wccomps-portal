# One image for both processes: they share the Django models and must run the same version.
#   web (default): entrypoint.sh applies migrations under a lock, then gunicorn (web/gunicorn.conf.py)
#   bot: entrypoint cleared, run from /app: `python main.py`
# The Python .python-version pins (local uv and CI read it too): a test that passes on one patch can fail on another.
# uv sync below reads the file and, with downloads off, refuses an image whose Python differs; test_python_pin checks
# this tag against it.
# Each image is a FROM with a version tag and its digest, which Dependabot's docker entry bumps together
# (test_python_pin). Python's tag changes only with .python-version; Dependabot refreshes just its digest.

# uv only installs, so it's mounted for that step instead of shipped
FROM ghcr.io/astral-sh/uv:0.13.5@sha256:32a7bb39b496babbd997b0a39a4d66c126bdaf5661b302bf6f06abda8deca6ff AS uv

FROM python:3.14.8-slim@sha256:fbecd461fc72d21b2143c6eaba25143738079154a6d0ea3da3aec724f50eed4a

WORKDIR /app

# WeasyPrint's libraries (Pango, Cairo, GDK-Pixbuf) for PDFs. Every Python dependency installs from a
# wheel, so no compiler or headers.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libgdk-pixbuf-2.0-0 \
    libcairo2 \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock .python-version ./

# --no-build refuses a source distribution, which would need the compiler this image doesn't have
RUN --mount=from=uv,source=/uv,target=/bin/uv \
    UV_PYTHON_DOWNLOADS=never uv sync --frozen --no-dev --no-build

COPY web/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

COPY main.py ./main.py
COPY bot/ ./bot/
COPY web/ ./web/

ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONPATH=/app/web
ENV DJANGO_SETTINGS_MODULE=portal.settings
# Writable cache home (WeasyPrint/fontconfig) when running as a non-root user on a read-only root
ENV HOME=/tmp

WORKDIR /app/web

# Bake static files (web/static plus package assets like the admin's, hashed and compressed) into
# the image. Needs no database.
RUN python manage.py collectstatic --noinput

# Production's pod spec sets this uid too (runAsUser 1001). The app writes only to /tmp (uploads, gunicorn
# heartbeats, fontconfig) and the bot's heartbeat directory under /app/run.
RUN groupadd --system --gid 1001 app \
    && useradd --system --uid 1001 --gid 1001 --home-dir /tmp --no-create-home --shell /usr/sbin/nologin app \
    && mkdir -p /app/run \
    && chown 1001:1001 /app/run
USER 1001:1001

ENTRYPOINT ["/entrypoint.sh"]
# Bind address, workers (GUNICORN_WORKERS), timeout and logging live in web/gunicorn.conf.py
CMD ["gunicorn", "portal.wsgi:application"]
