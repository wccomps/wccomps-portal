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
# Browser tests (Playwright): same env, `-m browser -n0`. CI doesn't run them (nightly-browser.yml does, on main after
# the merge), so run them by hand before a template or JS PR.
# Hypothesis runs the "ci" profile: 25 fixed examples per test (10 for the view and bot tests). HYPOTHESIS_PROFILE=deep
# runs 2000 random ones (500 for the view and bot tests), and runs nightly on main (deep-properties.yml). No test pins
# its own count. Each nightly workflow keeps one issue open while it fails and closes it when it passes (nightly.yml).
# GitHub turns a public repo's schedules off after 60 days with no repo activity (it emails first):
# `gh workflow list --all` shows each scheduled workflow's state, and `gh workflow enable <file>` turns one back on.
# Input and external-JSON property tests: web/core/tests/test_properties_*.py and bot/tests/test_properties_bot.py;
# test_property_coverage.py fails on any entry point without one (or an exceptions entry with a reason). A new URL
# pattern needs a row in its test_properties_views_* table (views_harness.ViewCase); a new bot command, component or
# listener needs a row in bot/tests/test_properties_bot.py (CASES); a new external JSON reader needs a case in
# test_properties_readers.py (READERS).

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
- Starts, stops and Close Out share the Postgres advisory lock `COMPETITION_RUN_LOCK_ID`. If a stuck run holds it,
  find the holder with `SELECT pid FROM pg_locks WHERE locktype = 'advisory' AND classid = 1 AND objid = 2966672707`
  and end it with `pg_terminate_backend(pid)`.

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
- A role check is `@require_permission("role_name")`, pages and JSON endpoints alike: signed out, it sends to
  sign in; signed in without the role, it raises `PermissionDenied` (the 403). A view open to staff or a team's
  own members (tickets, incident reports) checks in `team_record_or_404` or raises `PermissionDenied` itself
- Views raise `PermissionDenied`, `Http404` and `BadRequest("why")`; the handlers in `core.errors` answer the
  error page, or `{"error": ...}` with the same status for a JSON endpoint (`@json_endpoint`), a script that asks
  for JSON (`Accept: application/json`) or a fetch. A wrong method is `require_POST`/`require_http_methods`.
  test_no_hand_built_errors fails on a view building its own error answer; its exceptions are the stated ones (the
  sign-in and Discord-link flows' own pages, a form re-rendered with its errors, the IP pool modal's field errors)
- A refusal that is a record's state, not a role (a ticket someone else holds, an approved report), redirects back
  with a message
- A team's record (ticket, incident report, attachment) is looked up with `team_record_or_404`: another team's
  record is a 404, the same as a missing one, never a 403
- `WCComps_Discord_Admin` grants access to everything

### Discord Task Queue
- Web code queues work with `DiscordTask.enqueue(<payload>)`; the bot's `DiscordQueueProcessor` consumes it
- Each task type is a payload dataclass in `core/discord_tasks.py` (the `TaskPayload` union); a new
  one also needs a `case` in `DiscordQueueProcessor._dispatch`, which mypy checks is exhaustive, and a
  migration, since `DiscordTask.task_type`'s choices come from the union
- A payload's `ticket_id` also sets the task's ticket FK, so deleting a ticket drops its pending tasks
- A handler's return value is stored in `DiscordTask.result`; the payload is input only

### UI Components
- Pages extend `admin/base_site.html`, directly or through a section base (`admin/ops_base.html`,
  `scoring/base.html`, `orange_team/base.html`, ...)
- Use django-cotton components from `templates/cotton/`
- `core/cotton.py` escapes every value a component writes with `{{ attrs }}` (see "Attributes and Escaping" in
  `templates/cotton/README.md`); run `core/tests/test_cotton_escaping.py` after a django-cotton upgrade.
  Bind Alpine on a component with `::name`: `:name` reads a page variable
- A form's field renders itself: `{{ form.x.as_field_group }}` (`templates/forms/field.html`); labels and help text
  live on the form. `c-form_field` is for an input that is no form's field
- One page's htmx-refreshed markup (a list's table) is an `{% include %}` beside the page, not a component

### Streaming Progress Pattern
- Use `StreamingHttpResponse` + NDJSON + Alpine.js for long operations
- Use `core.utils.ndjson_progress()` helper for progress lines
- Wrap any stream that changes data in `core.utils.run_detached()`, so it finishes even when the browser
  or proxy disconnects (read-only streams don't need it)

### Team Model
- `MAX_TEAMS = 50` defined in `team/models.py`
- Team accounts are shared (multiple Discord users per Authentik account)
