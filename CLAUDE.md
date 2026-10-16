# CLAUDE.md

## Project Overview

WCComps Portal is a competition management platform for WRCCDC. Three components:
- **Discord Bot** (`main.py` + `bot/`) — Team ticketing, role sync, competition commands
- **Django Web** (`web/`) — Scoring portal, inject grading, packet distribution, ops dashboard
- **Authentik Integration** — SSO, team provisioning, permission sync via `core/authentik_manager.py`

## Quick Commands

```bash
# Run tests from the repo root (requires test DB: docker compose -f docker-compose.test.yml up -d --wait).
# From web/ pytest ignores testpaths and skips bot/tests.
PYTHONPATH="$(pwd)/web:$(pwd)" DB_HOST=localhost DB_PORT=5433 DB_USER=test_user DB_PASSWORD=test_password DB_NAME=wccomps_test uv run pytest -m "not browser"
# Browser tests (Playwright): same env, `-m browser -n0`
# Hypothesis runs the "ci" profile: 25 fixed examples per test (10 for the view tests). HYPOTHESIS_PROFILE=deep runs
# 2000 random ones (500 for the view tests), and runs nightly on main (deep-properties.yml keeps one issue open
# while it fails, closed when it passes). No test pins its own count. GitHub turns a public repo's schedule off after
# 60 days with no repo activity (it emails first): turn it back on under Actions, "Nightly deep property tests",
# "Enable workflow" (or `gh workflow enable deep-properties.yml`).
# User-input property tests: web/core/tests/test_properties_*.py; test_property_coverage.py fails on any input
# without one (or an exceptions entry with a reason). A new URL pattern needs a row in its test_properties_views_*
# table (views_harness.ViewCase).

# Lint and type checks (CI runs these plus the tests on every PR)
uv run ruff check . && uv run djlint web/templates --lint && DJANGO_SETTINGS_MODULE=portal.settings uv run mypy
```

## Production

Runs on the deoxys Kubernetes cluster, deployed by Argo CD from `wccomps/wccomps-argocd`
(`manifests/wccomps-portal/`; its README covers operations).
- CI builds one image for web and bot on every merge to main: `ghcr.io/wccomps/wccomps-portal:sha-<short>`.
- Deploy: PR to wccomps-argocd bumping `newTag` in `manifests/wccomps-portal/kustomization.yaml`.
- After changing its `configmap.yaml` or `secrets.yaml`, restart the web and bot Deployments.
- `docker-compose.yml` is for anyone self-hosting the public repo (Postgres + web + bot); it isn't
  how production runs.

## Python 3.14

This project targets Python 3.14+ (`requires-python = ">=3.14"`). One patch version runs everywhere:
`.python-version` (uv reads it locally and in the image build, CI's setup-python reads it) and the Dockerfile's base
image name the same one; change them together (a test can pass on one patch and fail on another). The build refuses
a mismatch and `test_python_pin` checks the tag. PEP 758 re-enables
`except A, B:` syntax without parentheses. Do NOT change these to `except (A, B):`:
the unparenthesized form is used intentionally throughout.

## Key Conventions

### Permissions
- All authorization uses Authentik groups via `core.permission_constants.PERMISSION_MAP`
- Use `@require_permission("role_name")` decorator for page views
- Use manual `has_permission()` check for JSON API endpoints (return `JsonResponse` 403)
- `WCComps_Discord_Admin` grants access to everything

### Discord Task Queue
- Web code queues work with `DiscordTask.enqueue(<payload>)`; the bot's `DiscordQueueProcessor` consumes it
- Each task type is a payload dataclass in `core/discord_tasks.py` (the `TaskPayload` union); a new
  one also needs a `case` in `DiscordQueueProcessor._dispatch`, which mypy checks is exhaustive, and a
  migration, since `DiscordTask.task_type`'s choices come from the union
- A payload's `ticket_id` also sets the task's ticket FK, so deleting a ticket drops its pending tasks
- A handler's return value is stored in `DiscordTask.result`; the payload is input only

### UI Components
- Pages extend `admin/base_site.html`, directly or through a section base (`admin/base.html`,
  `scoring/base.html`, `orange_team/base.html`, ...)
- Use django-cotton components from `templates/cotton/`

### Streaming Progress Pattern
- Use `StreamingHttpResponse` + NDJSON + Alpine.js for long operations
- Use `core.utils.ndjson_progress()` helper for progress lines
- Wrap any stream that changes data in `core.utils.run_detached()`, so it finishes even when the browser
  or proxy disconnects (read-only streams don't need it)

### Team Model
- `MAX_TEAMS = 50` defined in `team/models.py`
- Team accounts are shared (multiple Discord users per Authentik account)
