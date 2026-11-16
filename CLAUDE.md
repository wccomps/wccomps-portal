# CLAUDE.md

## Project Overview

WCComps Portal is a competition management platform for WRCCDC. Four components:
- **Chat process** (`python -m chat`, `chat/__main__.py`): a runner per configured chat backend
  (`core.heartbeat.CHAT_BACKENDS`) in one event loop: the Discord bot (`bot/`: team ticketing, role sync, competition
  commands) and the Zulip runner (`chat/zulip/`: ticket topics, notices, replies as comments)
- **Worker** (`worker/`, `python -m worker`): competition timer, Authentik group refresh, Quotient sync, worker queue
- **Django Web** (`web/`): scoring portal, inject grading, packet distribution, ops dashboard
- **Authentik Integration**: SSO, team provisioning, permission sync via `core/authentik_manager.py`

## Quick Commands

```bash
# Run tests from the repo root (requires test DB: docker compose -f docker-compose.test.yml up -d --wait).
# From web/ pytest ignores testpaths and skips bot/tests, chat/tests and worker/tests.
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
- CI builds one image for web, worker and chat on every merge to main: `ghcr.io/wccomps/wccomps-portal:sha-<short>`.
- Deploy: PR to wccomps-argocd bumping `newTag` in `manifests/wccomps-portal/kustomization.yaml`.
- After changing its `configmap.yaml` or `secrets.yaml`, restart the web, worker and bot (chat) Deployments.
- The worker and chat pods have no HTTP listener; their liveness probe is `python -m core.health worker|chat`, which
  checks the heartbeats that process writes (`core.heartbeat.budgets`).
- The configmap sets WRCCDC's names: group prefix and admin group, org names, hosts, Authentik, Quotient and email
  addresses. `web/portal/settings.py` defaults them neutral (`Portal_`, empty); the web server (`portal/wsgi.py`),
  the worker (`worker/__main__.py`) and the chat process (`chat/__main__.py`) refuse to start while a setting in
  `portal.required.REQUIRED_SETTINGS` is empty. The test suite runs as WRCCDC's deployment:
  `portal/test_settings.py` sets the configmap's values.
- `docker-compose.yml` is for anyone self-hosting the public repo (Postgres + web + worker, and chat under the
  `chat` profile); it isn't how production runs.
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
- `core.auth_utils.groups_grant` decides every permission, web and bot, from the Authentik groups in
  `core.permission_constants.PERMISSION_MAP`; a TEAM_HELD permission (blue_team) also holds for an active team's
  group (`Team.is_held_by`, `TeamQuerySet.held_by`). A disabled account holds no permissions but keeps its team's Discord roles
- A role check is `@require_permission("role_name")`, pages and JSON endpoints alike: signed out, it sends to
  sign in; signed in without the role, it raises `PermissionDenied` (the 403). A view open to staff or a team's
  own members (tickets, incident reports) checks in `team_record_or_404` (the ticket page in
  `ticketing.access.ticket_open_to`, which the bot shares) or raises `PermissionDenied` itself
- Views raise `PermissionDenied`, `Http404` and `BadRequest("why")`; the handlers in `core.errors` answer the
  error page, or `{"error": ...}` with the same status for a JSON endpoint (`@json_endpoint`), a script that asks
  for JSON (`Accept: application/json`) or a fetch. A wrong method is `require_POST`/`require_http_methods`.
  test_no_hand_built_errors fails on a view building its own error answer; its exceptions are the stated ones (the
  sign-in and Discord-link flows' own pages, a form re-rendered with its errors, the IP pool modal's field errors)
- A refusal that is a record's state, not a role (a ticket someone else holds, an approved report), redirects back
  with a message
- A team's record (ticket, incident report, attachment) is looked up with `team_record_or_404`: another team's
  record is a 404, the same as a missing one, never a 403
- `PORTAL_ADMIN_GROUP` (default `Portal_Admin`, WRCCDC's `WCComps_Discord_Admin`) grants access to everything; each
  other role's group is `PORTAL_GROUP_PREFIX` (default `Portal_`, WRCCDC's `WCComps_`) plus a fixed suffix, and a new
  team's group is `TEAM_GROUP_PATTERN` (after that, `Team.authentik_group`)

### Task Queue
- Web code queues work with `QueuedTask.enqueue(<payload>)`, which writes one row per backend
  (`core.task_backends.TaskBackend`) and returns them; `enqueue(payload, origin=...)` skips the backend the payload
  came from (a comment made in a Discord thread is already there). Each backend's `core.task_queue.QueueProcessor`
  subclass consumes its own rows: the bot's `DiscordQueueProcessor` the `discord` ones,
  `chat.zulip.outgoing.ZulipQueueProcessor` the `zulip` ones (both in the chat process),
  `core.worker_tasks.WorkerQueueProcessor` the `worker` ones, in the worker process (`python -m worker`, beside the
  timer, group refresh and Quotient sync in `worker/jobs.py`)
- Each task type is a payload dataclass in `core/queued_tasks.py`, in one group of the `TaskPayload` union:
  `ChatNotice` (a row for every configured chat backend, `core.task_backends.configured_chat_backends`: each one in
  `core.heartbeat.CHAT_BACKENDS` whose settings are all set, on the process that queues), `DiscordOnly` (a row only
  when Discord is configured) or `WorkerTask` (always a `worker` row); so no configured chat backend means no chat row.
  `BACKEND_TASKS` maps each backend to its groups. A new one also needs a `case` in its backends' `dispatch` (each
  processor is a `QueueProcessor[P]` over its groups, and mypy checks its match exhaustive over them; a row outside
  them fails at once), and a migration, since `QueuedTask.task_type`'s choices come from the union
- What a chat notice says (a ticket update, a ticket's facts, the competition status) lives in `core.chat_notices`;
  each chat backend only renders it (Discord as embeds, Zulip as Markdown)
- An ops notice goes through `core.services.ops.notify_ops` (audited, then a `LogToChannel` row), never a direct
  `LogToChannel` enqueue
- A payload's `ticket_id` also sets the task's ticket FK, so deleting a ticket drops its pending tasks
- A handler's return value is stored in `QueuedTask.result`; the payload is input only
- A handler raises `PermanentTaskError` (`core.queued_tasks`) to fail without retrying, and looks up the record it
  is about with `_target`, which raises it; a handler whose retry would repeat a send or a destructive step takes a
  `TaskProgress` (bound to its task: `still_running()` says whether a reset ended it meanwhile), so a retry resumes
  from what `QueuedTask.progress` records; the rest are written to be rerun

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
  or proxy disconnects (read-only streams don't need it); `start_detached()` runs one with no reader,
  starting once the caller's transaction commits

### Team Model
- `MAX_TEAMS` (setting, default 50) is read into `team/models.py`
- A team's Authentik accounts come from `TEAM_ACCOUNT_PATTERN` (`core/team_accounts.py`): a pattern naming one
  username (`team.models.single_account()`) is the team's one shared account, by default `teamNN` (multiple Discord
  users per Authentik account); otherwise they are the members of its group matching it. Password resets need the
  one account and are refused when teams have several
