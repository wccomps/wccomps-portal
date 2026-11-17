# Self-hosting the portal

How to run the portal for your own competition: the processes, the Authentik setup it needs, the optional
integrations, Discord and Zulip, and every setting. Settings are environment variables; `.env.example` lists them all,
grouped as below, with a working or empty value.

## The processes

One image (`ghcr.io/wccomps/wccomps-portal`) runs three processes against one Postgres database. They must run the same
version, since they share the models.

| Process | Command | What it does | Liveness |
|---|---|---|---|
| web | `gunicorn portal.wsgi:application` (the image's default) | The portal's pages: tickets, scoring, inject grading, packets, registration, the ops pages. Its entrypoint (`web/entrypoint.sh`) first runs `manage.py prepare_database`: migrations under a lock, then teams 1 to `MAX_TEAMS` if there are none. | `GET /livez/` (no database); `GET /health/` also queries the database |
| worker | `python -m worker` | The competition timer (scheduled starts and stops), the Authentik group refresh every 5 minutes, the Quotient metadata sync every 5 minutes (15 while Quotient is unreachable), which also reads Proxmox and pushes to aweful-proxy, and the worker queue: Nextcloud accounts and the Quotient sync a competition start queues. It talks to no chat system. | `python -m core.health worker` |
| chat | `python -m chat` | A runner for each configured chat backend, in one event loop: the Discord bot when `DISCORD_BOT_TOKEN` is set, the Zulip runner when `ZULIP_SITE`, `ZULIP_BOT_EMAIL` and `ZULIP_API_KEY` are all set. It exits at once when neither is configured. | `python -m core.health chat` |

The web server, the worker and the chat process refuse to start while any of `AUTHENTIK_URL`, `DEFAULT_FROM_EMAIL`,
`ORG_NAME` or `ORG_SHORT_NAME` is empty (`web/portal/required.py`). The web server also refuses without
`DJANGO_SECRET_KEY`, or with the placeholder `.env.example` ships.

Run exactly one worker and exactly one chat process per deployment. Each queue consumer, at its start, puts back the
tasks its backend left half done (`recover_stranded` in `web/core/task_queue.py`); a second copy would take a task the
first is still running and run it twice, and the worker's timer and sync loops would run twice too. Web can have
several replicas.

### With Docker Compose

`docker-compose.yml` runs Postgres, web and the worker; the chat process is under the `chat` profile.

```bash
cp .env.example .env                  # fill it in
docker compose up -d                  # Postgres, web and worker from the published image
docker compose --profile chat up -d   # also the chat process, once DISCORD_BOT_TOKEN or the ZULIP_* trio is set
```

`docker compose up -d --build` runs the checkout instead; `PORTAL_TAG` picks the published tag (default `main`). The
web app listens on `localhost:${WEB_PORT:-8000}`. Put it behind an HTTPS reverse proxy: with `DJANGO_DEBUG=False` the
session and CSRF cookies are HTTPS-only. The worker and chat services have the health checks above.

### Without Docker

With Python 3.14 and [uv](https://docs.astral.sh/uv/), from the repository root, with the settings in the environment
(the settings read the environment; nothing loads `.env` itself):

```bash
uv sync --no-dev
set -a; . ./.env; set +a
export PYTHONPATH="$PWD/web" BOT_HEARTBEAT_DIR="$PWD/run/heartbeat"
(cd web && uv run python manage.py prepare_database && uv run python manage.py collectstatic --noinput)
(cd web && uv run gunicorn portal.wsgi:application)   # web/gunicorn.conf.py binds 0.0.0.0:8000
uv run python -m worker                               # in another shell
uv run python -m chat                                 # in another shell
uv run python -m core.health worker                   # exits 0 while the worker's heartbeats are fresh
```

WeasyPrint (scorecard PDFs) needs Pango, Cairo and GDK-Pixbuf from the system; the `Dockerfile` lists the Debian
packages.

## Authentik

Authentik is the only sign-in and the only source of roles. The portal needs an OAuth2/OpenID provider to sign in with,
and an API token for everything else.

### The sign-in provider

1. Create an OAuth2/OpenID provider (confidential client) and an application for it. Allow the redirect URI
   `https://<portal host>/auth/callback/` for each host the portal is reached on.
2. The portal asks for the scopes `openid profile email groups` and reads `sub`, `preferred_username`, `email` and
   `groups` from the userinfo answer. `groups` must list the user's Authentik group names: Authentik's default
   `profile` scope mapping returns them; if you replaced the default mappings, add one that does. `sub` is the
   account's key, `preferred_username` its portal username.
3. Set `AUTHENTIK_CLIENT_ID` and `AUTHENTIK_SECRET` to the provider's client ID and secret, and `AUTHENTIK_OIDC_URL` to
   `https://<authentik>/application/o/<application slug>/`. The portal builds the authorize, token, userinfo and
   end-session URLs from it (no discovery); it defaults to `AUTHENTIK_URL/application/o/discord-bot/`.

A user's groups are read at each sign-in and again by the worker every 5 minutes, so a group change takes effect within
5 minutes (at once on the next sign-in). A disabled Authentik account holds no permissions.

### The API token

`AUTHENTIK_TOKEN` is a token of an Authentik service account whose role holds the permissions below
(`web/core/authentik_manager.py`). Authentik lists only what a token may view, so a missing view permission shows up
as an empty list, not an error; run the readiness checks on the Competition page after setting it up.

| Object | Permissions | Used for |
|---|---|---|
| Users | view, change, reset password | Reading every user and their groups (group refresh, team accounts), enabling and disabling team accounts at a start and stop, storing a linked Discord ID in the user's attributes, setting team passwords |
| Groups | view, add user | Reading group names; the readiness fix that puts a team account in its team group |
| Authenticated sessions | view, delete | Ending a team account's sessions when its credentials are reset. The readiness check "Authentik sessions visible" tests the view permission (it passes only when the token sees at least one session, so sign in to Authentik first); it can't test delete |
| Applications | view | Finding each controlled application by slug |
| Providers | view (`authentik_core.view_provider`, global) | Listing applications to choose controlled applications from |
| Policy bindings | view, change | Enabling and disabling each controlled application's blue team binding |

The token can't create accounts or groups: create them in Authentik.

### Permission groups

Each role's group is `PORTAL_GROUP_PREFIX` (default `Portal_`) plus a fixed suffix (`web/core/permission_constants.py`
`PERMISSION_MAP`); the admin group is its own setting. Create the ones you use:

| Group | Grants |
|---|---|
| `PORTAL_ADMIN_GROUP` (default `Portal_Admin`) | Everything, including Django's admin at `/admin/` |
| `Portal_GoldTeam` | Gold team, and every white, orange and blue team permission: the competition and its setup, teams and their accounts, broadcasts, packets, scorecards, ticket categories |
| `Portal_WhiteTeam`, `Portal_WhiteTeam_Lead` | White team pages (inject grading, incident reports); the lead adds the white team lead's |
| `Portal_OrangeTeam`, `Portal_OrangeTeam_Lead` | Orange team pages; the lead adds the orange team lead's |
| `Portal_RedTeam` | Red team findings |
| `Portal_Ticketing_Support` | Working tickets |
| `Portal_Ticketing_Admin` | Ticketing support, plus ticket administration and ticket categories |
| `Portal_BlackTeam` | Only its Discord role |
| A team's group (below) | That team's blue team access: its own tickets and packets |

With Discord, the black, white, orange, red and gold team groups also grant the Discord role in `BLACKTEAM_ROLE_ID`,
`WHITETEAM_ROLE_ID` and so on.

### Teams and team accounts

`prepare_database` creates teams 1 to `MAX_TEAMS` (default 50) the first time. A new team's Authentik group is
`TEAM_GROUP_PATTERN` (default `PORTAL_GROUP_PREFIX` plus `BlueTeam{number:02d}`, so `Portal_BlueTeam01`); after that
each team keeps the group stored on it (editable in Django's admin). An active team's group grants blue team access
to its members.

`TEAM_ACCOUNT_PATTERN` (default `team{number}`) says which Authentik accounts are a team's; `{number}` stands for the
team's two-digit number.

- **One shared account per team** (the default): a pattern with no regular expression operator names the team's one
  account by exact username (`team01`, `team02`, ...). Every member of the team signs in with it, and the portal can
  reset its password (ending its sessions and Discord links) and email it in the team's packet. Put each account in
  its team's group; the readiness checks find one that isn't and offer to fix it.
- **Several accounts per team**: a pattern with a regular expression operator, such as `team{number}[a-i]`, makes a
  team's accounts the members of its group whose username fully matches it. The portal's password resets and the
  packet's credentials are off; manage passwords in Authentik.

A competition start enables each active team's accounts and opens the controlled applications; a stop disables every
team's accounts and closes them. Create the accounts in Authentik before the event; the readiness checks list the
missing ones.

### Controlled applications

The Authentik applications teams get only while the competition runs (a VPN, a remote desktop gateway) are its
controlled applications, chosen on the Competition setup page (`/ops/admin/competition/setup/`, "Team access"). Each
one needs a policy binding to a group whose name contains `blueteam` (any case), such as a group holding the team
groups; a start enables that binding and a stop disables it. A start is refused while the list is empty, and the
readiness check "App bindings configured" fails for an application without such a binding.

## Optional integrations

Each of these is off until its settings are set; the rest of the portal works without it.

### Quotient

The Quotient scoring engine supplies the box and service lists (ticket forms, red team and incident forms, bot
autocomplete), inject titles for grading, ticket service check history, and the service scores the competition stop
pulls in. Set `QUOTIENT_API_URL` and an `[[admin]]` account from Quotient's `event.conf` as `QUOTIENT_USERNAME` and
`QUOTIENT_PASSWORD`.

- A Quotient team's number is the digits in its name (`team09` is team 9); a name without digits is skipped.
- An inject's point value comes from its title: `(10 pts)`, `(1,000 points)`.
- A ticket's service is matched to Quotient's check `box-service`.

Without Quotient, inject grading says Quotient is unavailable, box lists stay empty, the stop's score sync step fails
(the stop still completes), and the worker logs the failed sync every 15 minutes.

### Proxmox

With Proxmox, staff reset a box from a ticket in the box reset category (`BOX_RESET_CATEGORY_ID`, the seeded "Box
Reset / Scrub"): the VM is rolled back to a snapshot (`PROXMOX_DEFAULT_SNAPSHOT` is offered first) and started, and the
ticket is resolved. The Quotient sync also adds the team VMs Quotient doesn't score (routers, firewalls) to the box
lists.

- `PROXMOX_API_URL` is one host or several, comma-separated (a GET moves on to the next when one is down);
  `PROXMOX_API_TOKEN` is `user@realm!tokenid=secret`.
- The team VMs follow one layout (`web/core/proxmox.py`): team N's VMs are in the pool `blue-team-NN`, with VMIDs from
  `10000 + N*100` to `10000 + N*100 + 99`, named `teamNN-<host>`. Tags aren't used.
- The token reads pools, lists, rolls back and starts VMs, and reads task status. Proxmox's documentation names the
  privileges for those calls (Pool.Audit, VM.Audit, VM.Snapshot.Rollback, VM.PowerMgmt, Sys.Audit); a
  privilege-separated token needs them on the token itself.
- `PROXMOX_CA_FILE` trusts the cluster's own CA; `PROXMOX_VERIFY_SSL=False` turns verification off.

Without Proxmox, a box reset ticket shows Proxmox as unreachable and is resolved by hand.

### aweful-proxy

Each Quotient metadata sync posts the team network layout it reads from Quotient (team count, address masks, the team
DNS server and domain) to `AWEFUL_PROXY_URL/config`, with `AWEFUL_PROXY_TOKEN` as a Bearer token when set. A failed
post is only logged. Without `AWEFUL_PROXY_URL` nothing is posted.

### Nextcloud

The worker gives each team a Nextcloud account through the `user_oidc` app, so team members sign in to Nextcloud
through Authentik. The account's name is the team's account username (`team01`; with several accounts per team,
`teamNN`).

- A competition start makes the active teams' accounts; opening an event remakes them; Close Out and the Discord
  `/competition cleanup-competition` delete them with their files; handing a team to a new school remakes its account.
- Set `NEXTCLOUD_URL`, a Nextcloud admin as `NEXTCLOUD_USERNAME` and `NEXTCLOUD_PASSWORD`, and
  `NEXTCLOUD_OIDC_PROVIDER_ID` to the numeric id of the Authentik provider in `user_oidc` (default 1).

It runs in the worker, so it works with or without a chat backend. Without the three settings the Nextcloud steps are
skipped.

### Email

Packets and scorecards are emailed from `DEFAULT_FROM_EMAIL`, packets with `DEFAULT_REPLY_TO_EMAIL` as Reply-To when
set. The default backend is SMTP with the `EMAIL_*` settings; `.env.example` sets the console backend, which prints each
message to the log instead.

### Error alerts

`DISCORD_ERROR_WEBHOOK_URL`, a Discord channel webhook (https only), gets an alert for each server error and each
error the portal logs to `wccomps.errors`. It works without the Discord bot.

## Chat

The chat backends are Discord and Zulip; run either, both, or neither (then tickets live in the portal only, and no
chat notices are queued). A backend is configured by its settings, and web and the worker need the same chat settings
as the chat process: the process that queues a notice writes one task row per backend configured there, and the chat
process consumes its backends' rows. Rows for a backend no chat process runs wait in the queue.

When both run, each ticket comment reaches both: a comment made in the portal is posted to the Discord thread and the
Zulip topic, and one made in Discord or Zulip is saved on the ticket and posted to the other. Edits and deletes are
not mirrored between them: an edit or delete in one updates the portal's comment, and the other system's copy keeps
its original text. Files posted in one are stored on the ticket and not re-posted to the other.

### Discord

1. Create a Discord application with a bot, and turn on its privileged intents **Server Members** and **Message
   Content**. Set `DISCORD_BOT_TOKEN` to its token.
2. Invite it to the competition server (`DISCORD_GUILD_ID`) and, if staff link from a separate one, the volunteer server
   (`VOLUNTEER_GUILD_ID`, where only `/link` is registered). The bot creates and deletes team roles, categories and
   channels, sets their permissions, makes private ticket threads, pins and deletes messages and assigns roles, so
   give it Manage Roles, Manage Channels, Manage Threads, Create Private Threads, Manage Messages, Send Messages,
   Send Messages in Threads, Embed Links, Attach Files and Read Message History. Its role must be above every role it
   assigns.
3. Create the roles and channels and set their IDs (Developer Mode, right-click, Copy ID). Each ID defaults to 0, which
   turns off what needs it; at startup the bot logs the required ones still at 0 and posts them to the ops log channel,
   and the readiness checks list them.

| Setting | What it is |
|---|---|
| `DISCORD_LOG_CHANNEL_ID` | Ops log: role sync summaries, failed tasks, admin command audits, competition notices |
| `DISCORD_TICKET_QUEUE_CHANNEL_ID` | Staff ticket dashboard, one message refreshed every 10 seconds |
| `DISCORD_ANNOUNCEMENT_CHANNEL_ID` | Broadcasts to the announcements target, mentioning the blue team role |
| `DISCORD_WELCOME_CHANNEL_ID` | Optional: the ticket panel (a Create Ticket button) |
| `DISCORD_LINK_CHANNEL_ID` | Optional: the Link Account panel; other messages posted there are deleted and answered with a link by DM |
| `BLUETEAM_ROLE_ID` | Every linked team member's role |
| `BLACKTEAM_ROLE_ID`, `WHITETEAM_ROLE_ID`, `ORANGETEAM_ROLE_ID`, `REDTEAM_ROLE_ID`, `GOLDTEAM_ROLE_ID` | The role for each Authentik group (`Portal_BlackTeam` and so on) |

Members run `/link`, sign in to the portal, and get their roles; a team member also gets the team's role (`Team NN`) and
category (`team NN`, with `teamNN-chat` and `teamNN-voice`), which the bot creates. Besides the white and orange team
roles, these roles see every team category when they exist, found by name: `<ORG_SHORT_NAME> Observers` (read only),
`Orange Team Guest`, `Black Team Guest`, `<ORG_SHORT_NAME> Room Judge` (read only), `<ORG_SHORT_NAME> Operations Team`,
and `DISCORD_SERVER_OWNER_ROLE` when set.

The bot syncs its slash commands when they change; `SYNC_COMMANDS=true` forces a sync at every start.

### Zulip

1. Create a bot in the Zulip realm and set `ZULIP_SITE` (https only), `ZULIP_BOT_EMAIL` and `ZULIP_API_KEY`.
2. Create the streams: one per team (`ZULIP_TEAM_STREAM`, default `team{number:02d}`), the staff ticket stream
   (`ZULIP_STAFF_STREAM`, `tickets`), announcements (`ZULIP_ANNOUNCEMENTS_STREAM`, `announcements`) and ops
   (`ZULIP_OPS_STREAM` and topic `ZULIP_OPS_TOPIC`, both `ops`). Subscribe the bot to all of them: Zulip delivers the
   bot only the streams it is subscribed to. A notice for a missing stream fails, and the ops stream is told to create
   it.
3. Let the bot see members' real email addresses: a realm email visibility setting that includes the bot, or an admin
   bot.

Each ticket gets a topic in its team's stream, named by its number and title, and a notice in the staff stream; a new
ticket mentions the user groups `ZULIP_TEAM_MENTION` (a pattern such as `Team {number:02d}`) and `ZULIP_STAFF_MENTION`
when set. Broadcasts go to the announcements stream or each team stream, ops notices and competition status to the ops
stream. The runner long-polls Zulip, so it needs no public address.

A message in a ticket's topic becomes a comment, and its uploads (up to 10) are stored on the ticket, only when the
sender's Zulip email matches, ignoring case, exactly one portal account that may write to the ticket: staff, or a
member of the ticket's team. A portal account exists from its first sign-in, with the email Authentik gives it, so
members sign in to the portal once and keep the same email in Zulip and Authentik. With one shared account per team,
that is the shared account's email, so only a member using it in Zulip can reply there; the others reply in the portal.
Anyone else is told so in the topic and nothing is saved. Tickets are opened in the portal (or Discord), not in Zulip.

The portal trusts the realm for this (`THREAT_MODEL.md`, accepted risks): the realm must not let anyone take an email
address they don't own, and the bot sees every message in the streams it is subscribed to.

## Ticket categories

The first migration seeds six categories: Service Scoring Validation, Box Reset / Scrub (the Proxmox box reset),
Scoring Service Check, Black Team Phone Consultation, Black Team Hands-on Consultation, and Other / General Issue, each
with its point cost and required fields. Edit, add or delete them at `/ops/admin/categories/` (gold team or ticketing
admin). If you replace Box Reset / Scrub, point `BOX_RESET_CATEGORY_ID` at the new category's ID.

## Settings reference

Every setting, with its default when unset. `.env.example` has the same groups.

### Required

| Setting | Default | Meaning |
|---|---|---|
| `ORG_NAME` | empty (refused) | Your organization's name; signs packet emails |
| `ORG_SHORT_NAME` | empty (refused) | Short name; heads email subjects and scorecards, and starts the Discord guest role names |
| `AUTHENTIK_URL` | empty (refused) | Authentik's address, for the API |
| `DEFAULT_FROM_EMAIL` | empty (refused) | From address of packet and scorecard emails |
| `DJANGO_SECRET_KEY` | random per process (the web server refuses) | Signs sessions, CSRF tokens and OAuth state; keep it fixed |
| `ALLOWED_HOSTS` | `localhost,127.0.0.1` | Host names the portal answers to, comma-separated |
| `BASE_URL` | `http://localhost:5000` | The portal's public address, for links in chat and email; an https one is CSRF's trusted origin |
| `AUTHENTIK_OIDC_URL` | `AUTHENTIK_URL/application/o/discord-bot/` | The sign-in provider's OpenID base |
| `AUTHENTIK_CLIENT_ID`, `AUTHENTIK_SECRET` | none (sign-in answers "OAuth is not configured") | The provider's client ID and secret |
| `AUTHENTIK_TOKEN` | empty | The API token |
| `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` | `localhost`, `5432`, `wccomps`, `wccomps`, `wccomps` | Postgres. Compose sets `DB_HOST` and `DB_PORT` itself and creates the database with the other three |

### Optional

| Setting | Default | Meaning |
|---|---|---|
| `DJANGO_DEBUG` | `False` | `True` only on a developer's machine; it also lets the cookies go over plain HTTP |
| `TRUST_CF_CONNECTING_IP` | off | `true` when every request comes through Cloudflare, so rate limits and the access log use its client address |
| `WEB_PORT` | `8000` | Port Compose publishes the web app on |
| `PORTAL_TAG` | `main` | Image tag Compose runs |
| `GUNICORN_WORKERS` | `4` | Gunicorn workers; each holds a database connection |
| `EMAIL_BACKEND` | `django.core.mail.backends.smtp.EmailBackend` | Django email backend |
| `EMAIL_HOST`, `EMAIL_PORT` | `localhost`, `587` | SMTP server |
| `EMAIL_USE_TLS`, `EMAIL_USE_SSL` | `True`, `False` | SMTP encryption |
| `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD` | empty | SMTP login |
| `DEFAULT_REPLY_TO_EMAIL` | empty (none) | Reply-To of packet emails |
| `SERVER_EMAIL` | `DEFAULT_FROM_EMAIL` | Django's error mail sender; the portal sets no `ADMINS`, so Django sends none |
| `QUOTIENT_API_URL`, `QUOTIENT_USERNAME`, `QUOTIENT_PASSWORD` | empty (off) | Quotient |
| `PROXMOX_API_URL`, `PROXMOX_API_TOKEN` | empty (off) | Proxmox hosts and token |
| `PROXMOX_CA_FILE` | empty (system CAs) | CA file for the Proxmox hosts |
| `PROXMOX_VERIFY_SSL` | `True` | Verify the Proxmox certificate (only the exact text `True` turns it on) |
| `PROXMOX_DEFAULT_SNAPSHOT` | `clean` | Snapshot a box reset offers first |
| `BOX_RESET_CATEGORY_ID` | `2` | The ticket category with the box reset |
| `AWEFUL_PROXY_URL`, `AWEFUL_PROXY_TOKEN` | empty (off) | aweful-proxy |
| `NEXTCLOUD_URL`, `NEXTCLOUD_USERNAME`, `NEXTCLOUD_PASSWORD` | empty (off) | Nextcloud and its admin login |
| `NEXTCLOUD_OIDC_PROVIDER_ID` | `1` | The Authentik provider's id in Nextcloud's `user_oidc` app |
| `HTTPX_DEFAULT_TIMEOUT` | `10` | Seconds for outside calls: Authentik, Proxmox, Nextcloud, aweful-proxy, Zulip, the Quotient login |
| `DISCORD_WEBHOOK_TIMEOUT` | `5` | Seconds for the error alert post |
| `DISCORD_ERROR_WEBHOOK_URL` | empty (off) | Discord webhook for error alerts; must be https |
| `BOT_HEARTBEAT_DIR` | `/app/run/heartbeat` | Where the worker and chat write the heartbeats `core.health` reads; set it to a writable directory outside the image |

### Discord

| Setting | Default | Meaning |
|---|---|---|
| `DISCORD_BOT_TOKEN` | empty (Discord off) | The bot's token; any value configures Discord |
| `DISCORD_GUILD_ID` | `0` | Competition server |
| `VOLUNTEER_GUILD_ID` | `0` | Volunteer server (`/link` only) |
| `DISCORD_LOG_CHANNEL_ID`, `DISCORD_TICKET_QUEUE_CHANNEL_ID`, `DISCORD_ANNOUNCEMENT_CHANNEL_ID` | `0` | Ops log, ticket dashboard and announcements channels |
| `DISCORD_WELCOME_CHANNEL_ID`, `DISCORD_LINK_CHANNEL_ID` | `0` (no panel) | Ticket panel and link panel channels |
| `BLUETEAM_ROLE_ID`, `BLACKTEAM_ROLE_ID`, `WHITETEAM_ROLE_ID`, `ORANGETEAM_ROLE_ID`, `REDTEAM_ROLE_ID`, `GOLDTEAM_ROLE_ID` | `0` | Role IDs |
| `DISCORD_SERVER_OWNER_ROLE` | empty (none) | Name of a role that may talk in every team category |
| `SYNC_COMMANDS` | off | `true` forces a slash command sync at startup |

### Zulip

| Setting | Default | Meaning |
|---|---|---|
| `ZULIP_SITE`, `ZULIP_BOT_EMAIL`, `ZULIP_API_KEY` | empty (Zulip off) | The realm (https) and the bot's email and API key; all three configure Zulip |
| `ZULIP_TEAM_STREAM` | `team{number:02d}` | Each team's stream, a pattern using `{number}` |
| `ZULIP_STAFF_STREAM` | `tickets` | New ticket notices for staff |
| `ZULIP_ANNOUNCEMENTS_STREAM` | `announcements` | Broadcasts to the announcements target |
| `ZULIP_OPS_STREAM`, `ZULIP_OPS_TOPIC` | `ops`, `ops` | Ops notices |
| `ZULIP_TEAM_MENTION` | empty (none) | User group a new ticket mentions in its team's stream, a pattern using `{number}` |
| `ZULIP_STAFF_MENTION` | empty (none) | Staff user group a new ticket mentions |

### Region names

| Setting | Default | Meaning |
|---|---|---|
| `SITE_NAME` | `<ORG_SHORT_NAME> Portal` | Page titles |
| `PORTAL_GROUP_PREFIX` | `Portal_` | Start of every role's Authentik group name |
| `PORTAL_ADMIN_GROUP` | `PORTAL_GROUP_PREFIX` plus `Admin` | The admin group |
| `TEAM_GROUP_PATTERN` | `PORTAL_GROUP_PREFIX` plus `BlueTeam{number:02d}` | A new team's Authentik group; must use `{number}` |
| `TEAM_ACCOUNT_PATTERN` | `team{number}` | A team's Authentik accounts; must contain `{number}` |
| `MAX_TEAMS` | `50` | Highest team number |

Changing a group, team account or team cap setting changes who holds which permission, and which accounts a start,
stop or reset touches.

## Operating notes

- One worker and one chat process per deployment; web may have several replicas (each migrates under a lock at start).
- Health: `/livez/` for web; `python -m core.health worker` and `python -m core.health chat` exit non-zero when a
  loop's heartbeat is stale or missing, so a stuck connection gets the container restarted. The chat check covers the
  backends configured in its environment.
- Queued work has one row per configured backend, written by the process that queues it: keep the chat settings the
  same on web, the worker and chat. A task that fails for good is reported to ops.
- Starts, stops and Close Out take one Postgres advisory lock, so only one runs at a time.
- Run the readiness checks on the Competition page before each event: Authentik accounts, groups, session access,
  application bindings, Discord settings, Quotient, packets and school info.
