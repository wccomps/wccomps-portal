# WCComps Portal

Competition management platform for WRCCDC: Discord bot, Django web app, and Authentik SSO integration.

## Components

- **Discord Bot** - Team ticketing, role sync, competition commands (the chat process)
- **Worker** - Competition timer, Authentik group refresh, Quotient sync, background tasks
- **Django Web** - Scoring portal, inject grading, packet distribution, ops dashboard
- **Authentik Integration** - SSO, team provisioning, permission sync

## Run with Docker

```bash
cp .env.example .env   # fill in credentials (DB password, Discord token, Authentik OAuth client)
docker compose up -d   # Postgres, web and worker from ghcr.io/wccomps/wccomps-portal; --build runs this checkout
docker compose --profile chat up -d   # also the chat process (the Discord bot), with DISCORD_BOT_TOKEN set
```

The web app listens on `localhost:${WEB_PORT:-8000}`. Put it behind an HTTPS reverse proxy: with
`DJANGO_DEBUG=False` the session and CSRF cookies are HTTPS-only. Run exactly one chat process per Discord token.

## Development

```bash
uv sync
docker compose -f docker-compose.test.yml up -d --wait   # Postgres on localhost:5433
uv run pytest

# Run the app locally (settings read the environment; they don't load .env themselves)
cp .env.example .env   # then fill in credentials; DB_* pointing at the Postgres above
set -a; . ./.env; set +a
cd web && uv run python manage.py prepare_database && uv run python manage.py runserver
uv run python -m worker  # the worker, from the repo root, in another shell
uv run python -m chat    # the chat process (Discord bot, Zulip runner, or both), from the repo root, in another shell
```

## Production

Runs on the deoxys Kubernetes cluster, deployed by Argo CD from `wccomps/wccomps-argocd`
(`manifests/wccomps-portal/`). CI publishes one image for web, worker and chat,
`ghcr.io/wccomps/wccomps-portal:sha-<short>`, on every merge to main; deploy by bumping its tag there.

## Troubleshooting

- **Bot not responding:** `kubectl -n wccomps-portal logs deploy/bot`
- **OAuth errors:** the Authentik provider must list `https://<host>/auth/callback/` for the host used
- **Permissions stale:** the worker re-reads everyone's Authentik groups every 5 minutes. A login refreshes that user's
  groups at once for the web; the bot's own permission cache can lag up to 5 minutes more
