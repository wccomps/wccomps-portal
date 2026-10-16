# One image for both processes: they share the Django models and must run the same version.
#   web (default): entrypoint.sh applies migrations under a lock, then gunicorn (web/gunicorn.conf.py)
#   bot: entrypoint cleared, run from /app: `python main.py`
# The Python .python-version pins (local uv and CI read it too): a test that passes on one patch can fail on another.
# uv sync below reads the file and, with downloads off, refuses an image whose Python differs; test_python_pin checks
# this tag against it.
FROM python:3.14.8-slim

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

# uv only installs, so it's mounted for this step instead of shipped. --no-build refuses a source
# distribution, which would need the compiler this image doesn't have.
RUN --mount=from=ghcr.io/astral-sh/uv:latest,source=/uv,target=/bin/uv \
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

ENTRYPOINT ["/entrypoint.sh"]
# Bind address, workers (GUNICORN_WORKERS), timeout and logging live in web/gunicorn.conf.py
CMD ["gunicorn", "portal.wsgi:application"]
