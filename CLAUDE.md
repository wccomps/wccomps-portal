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

# Lint and type checks (CI runs these plus the tests on every PR)
uv run ruff check . && uv run djlint web/templates --lint && DJANGO_SETTINGS_MODULE=portal.settings uv run mypy
```

## Production

Runs on the deoxys Kubernetes cluster, deployed by Argo CD from `wccomps/wccomps-argocd`
(`manifests/wccomps-portal/`; its README covers operations).
- CI builds one image for web and bot on every merge to main: `ghcr.io/wccomps/wccomps-portal:sha-<short>`.
- Deploy: run `./scripts/deploy.sh` (automates bumping `newTag` in `wccomps-argocd` via PR and merge).
- After changing its `configmap.yaml` or `secrets.yaml`, restart the web and bot Deployments.
- `docker-compose.yml` is for anyone self-hosting the public repo (Postgres + web + bot); it isn't
  how production runs.

## Python 3.14

This project targets Python 3.14+ (`requires-python = ">=3.14"`). PEP 758 re-enables
`except A, B:` syntax without parentheses. Do NOT change these to `except (A, B):` —
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
