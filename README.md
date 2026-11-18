# WCComps Portal

Competition management platform for WRCCDC: Discord bot, Django web app, and Authentik SSO integration.

## Components

- **Chat** - Team ticketing, role sync and competition commands in Discord (the bot), ticket topics and notices in
  Zulip, or both (the chat process)
- **Worker** - Competition timer, Authentik group refresh, Quotient sync, background tasks
- **Django Web** - Scoring portal, inject grading, packet distribution, ops dashboard
- **Authentik Integration** - SSO, team provisioning, permission sync

## Run with Docker

```bash
cp .env.example .env && docker compose up -d
```

[docs/self-hosting.md](docs/self-hosting.md) covers running your own: the processes, Compose and a plain run, the
reverse proxy, the Authentik setup, team accounts and permission groups, Quotient, Proxmox, Nextcloud and aweful-proxy,
Discord and Zulip, and every setting.

## Development

```bash
uv sync
docker compose -f docker-compose.test.yml up -d --wait   # Postgres on localhost:5433
DB_HOST=localhost DB_PORT=5433 DB_USER=test_user DB_PASSWORD=test_password DB_NAME=wccomps_test uv run pytest
```

To run the app locally, follow the guide's "Without Docker", with `uv sync` (dev dependencies included) and
`uv run python manage.py runserver` in `web/` in place of gunicorn.

## Production

Runs on the deoxys Kubernetes cluster, deployed by Argo CD from `wccomps/wccomps-argocd`
(`manifests/wccomps-portal/`). CI publishes one image for web, worker and chat,
`ghcr.io/wccomps/wccomps-portal:sha-<short>`, on every merge to main; deploy by bumping its tag there.

## Troubleshooting

- **Bot not responding:** `kubectl -n wccomps-portal logs deploy/bot`
- **OAuth errors:** the Authentik provider must list `https://<host>/auth/callback/` for the host used
- **Permissions stale:** the worker re-reads everyone's Authentik groups every 5 minutes. A login refreshes that user's
  groups at once, for the web and the bot alike
