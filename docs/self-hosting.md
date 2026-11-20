# Self-hosting the portal

How to run the portal for your own competition: the processes, the Authentik setup it needs, the optional
integrations, Discord and Zulip, and every setting. Settings are environment variables; `.env.example` lists them all,
and the [settings reference](#settings-reference) gives each one's default and meaning.

## The processes

One image (`ghcr.io/wccomps/wccomps-portal`) runs three processes against one Postgres database. They must run the same
version, since they share the models.

| Process | Command | What it does | Liveness |
|---|---|---|---|
| web | `gunicorn portal.wsgi:application` (the image's default) | The portal's pages: tickets, scoring, inject grading, packets, registration, the ops pages. Its entrypoint (`web/entrypoint.sh`) first runs `manage.py prepare_database`: migrations under a lock, then teams 1 to `MAX_TEAMS` if there are none. | `GET /livez/` (no database); `GET /health/` also queries the database |
| worker | `python -m worker` | The competition timer (scheduled starts and stops), the Authentik group refresh every 5 minutes, the Quotient metadata sync every 5 minutes (15 while Quotient is unreachable), which also reads Proxmox and pushes to aweful-proxy, the preflight report (Quotient's latest preflight round, queued for the ops channel 30 and 10 minutes before a scheduled start), the expired session cleanup (Django's clearsessions) every hour, and the worker queue: Nextcloud accounts and the Quotient sync a competition start queues. It talks to no chat system. | `python -m core.health worker` |
| chat | `python -m chat` | A runner for each configured chat backend, in one event loop: the Discord bot when `DISCORD_BOT_TOKEN` is set, the Zulip runner when `ZULIP_SITE`, `ZULIP_BOT_EMAIL` and `ZULIP_API_KEY` are all set. It exits at once when neither is configured. | `python -m core.health chat` |

The web server, the worker and the chat process refuse to start (`web/portal/required.py` `refuse_to_start`), naming
every reason at once, while:

- any of `AUTHENTIK_URL`, `DEFAULT_FROM_EMAIL`, `ORG_NAME` or `ORG_SHORT_NAME` is empty;
- `DJANGO_SECRET_KEY` is unset, the placeholder `.env.example` ships, shorter than 50 characters, or made of fewer
  than 5 distinct characters (Django's own `security.W009` rule);
- a service URL whose requests carry a credential isn't https: `AUTHENTIK_URL`, `AUTHENTIK_OIDC_URL`, and when set
  `NEXTCLOUD_URL`, `QUOTIENT_API_URL`, each host in `PROXMOX_API_URL`, and `AWEFUL_PROXY_URL` when
  `AWEFUL_PROXY_TOKEN` is set. A service reached over a network nobody else can read (an in-cluster address) may be
  plain http when `ALLOW_HTTP_URLS` names its setting, such as `ALLOW_HTTP_URLS=AWEFUL_PROXY_URL`.

A yes/no setting (`DJANGO_DEBUG`, `TRUST_CF_CONNECTING_IP`, `PROXMOX_VERIFY_SSL`, `EMAIL_USE_TLS`, `EMAIL_USE_SSL`)
takes `true`/`false`, `yes`/`no`, `on`/`off` or `1`/`0` in any case; empty is its default, and any other value stops
every process, so a typo can't quietly turn certificate checks or STARTTLS off.

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

`docker compose up -d --build` runs the checkout instead; `PORTAL_TAG` picks the published tag (a `sha-<commit>` tag
pins one build; `main` moves with every merge). Compose's Postgres refuses to start until `DB_PASSWORD` is set, and its
image is pinned by digest.

Compose publishes the web app on `127.0.0.1:8000` (`WEB_PORT`), for the reverse proxy below on the same host. Docker's
port rules get past host firewalls such as ufw, so publish it wider (`WEB_PORT=8000`, every interface) only on
purpose: a client that reaches gunicorn directly skips the proxy's request limits and, with `TRUST_CF_CONNECTING_IP`
on, can set its own address. The worker and chat services have the health checks above.

Web, the worker and the chat process run with a read-only root filesystem, no Linux capabilities and
`no-new-privileges`; each writes only to its `/tmp` (uploads, fontconfig) and `/app/run` (heartbeats), both tmpfs. The
chat service gets `.env` without `PROXMOX_API_TOKEN`, `AWEFUL_PROXY_TOKEN` and `EMAIL_HOST_PASSWORD`, which it never
uses, so a flaw in the code that reads Discord and Zulip messages can't reach them.

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

### The reverse proxy

Put the web app behind an HTTPS reverse proxy: with `DJANGO_DEBUG=false` the session and CSRF cookies are HTTPS-only.
The proxy must:

- pass the client's `Host` header through, and set `X-Forwarded-Proto: https`, replacing any value the client sent.
  The portal builds its sign-in redirect URI from them, and Authentik refuses one that isn't
  `https://<portal host>/auth/callback/`. The host must be in `ALLOWED_HOSTS`.
- unless it is Cloudflare, set `CF-Connecting-IP` to the client's address, replacing any value the client sent, with
  `TRUST_CF_CONNECTING_IP=true` on web. Otherwise the registration form's rate limit, which is per address, counts
  every client as the proxy, so a few registrations shut registration for everyone.
- cap the request body and read it before passing it on. The largest upload the portal takes is a 50 MB set of
  screenshots (`MAX_SUBMISSION_SIZE` in `web/core/stored_files.py`), so 55 MB leaves room for the form around it. Web
  runs 4 sync gunicorn workers, each held for a whole request, so a client sending a body slowly ties one up; nginx
  buffers the whole body first and gives up on a client that stalls.

With `TRUST_CF_CONNECTING_IP` on, clients can set their own address unless the origin is reachable only through the
proxy (or Cloudflare), so make it so. With nginx:

```nginx
proxy_set_header Host $host;
proxy_set_header X-Forwarded-Proto https;
proxy_set_header CF-Connecting-IP $remote_addr;
client_max_body_size 55m;
client_body_timeout 30s;
proxy_request_buffering on;
```

The portal itself refuses (400) a request whose form fields other than files come to more than 256 KB
(`DATA_UPLOAD_MAX_MEMORY_SIZE` in `web/portal/settings.py`), and each upload past its own limit: 10 MB for a ticket
attachment or screenshot, 25 MB for a packet, 50 MB for one submission's screenshots.

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
   end-session URLs from it (no discovery).

A user's groups are read at each sign-in and again by the worker every 5 minutes, so a group change takes effect within
5 minutes (at once on the next sign-in). A disabled Authentik account holds no permissions.

### The API token

`AUTHENTIK_TOKEN` is a token of an Authentik service account. Authentik lists only what a token may view, so a missing
view permission shows up as an empty list, not an error; run the readiness checks on the Competition page after
setting it up.

#### Authentik token permissions

Web, the worker and the chat process (which reads Discord and Zulip messages) all hold the token, so grant it no more
than the portal uses. Give the service account's role each permission below at the scope shown: a global permission
applies to every object of its kind, an object permission (the object's Permissions tab in Authentik's admin) only to
the objects named. Never give it reset password on all users, nor change on all policy bindings: a leaked token
holding them could set an Authentik admin's password, or remove the bindings that guard Authentik's own applications.
Nor change on all users, except as the team folder section below allows, an accepted risk in `THREAT_MODEL.md`.

| Object | Permission | Scope | Used for (`web/core/authentik_manager.py`) |
|---|---|---|---|
| Users | view | global | Reading every user and their groups (and, in team folder mode, their folders and attributes) for the group refresh, team accounts, team folder sign-ins and the readiness check "Usernames fixed" (`list_all_users`, `team_folder_pages`, `find_user`) |
| Users | change | object: each team account (`team01`, ...) | Enabling and disabling team accounts at a start and stop (`set_user_active`); storing a linked Discord ID in the account's attributes (`update_user_discord_id`) |
| Users | reset password | object: each team account | Setting team passwords (`set_password`) |
| Groups | view | global | Reading group names (and, in team folder mode, their attributes for the readiness check "Usernames fixed") (`list_all_groups`) |
| Groups | add user | object: each team group | The readiness fix that puts a team account in its team group (`add_user_to_group`) |
| Authenticated sessions | view, delete | global | Ending a team account's sessions when its credentials are reset (`user_sessions`, `end_sessions`, `sees_any_sessions`); see below |
| Applications | view | global | Finding each controlled application by slug (`get_application_by_slug`) |
| Providers | view (`authentik_core.view_provider`) | global | Listing applications to choose controlled applications from (`list_applications`) |
| Policy bindings | view | global | Finding each controlled application's blue team binding (`app_bindings`) |
| Policy bindings | change | object: each controlled application's blue team binding | Enabling and disabling it at a start and stop (`update_binding_enabled`) |
| System settings | view (`authentik_rbac.view_system_settings`) | global | Team folder mode only: the readiness check that users can't change their usernames (`users_may_change_username`) |
| OAuth2/OpenID providers | view | object: the portal's sign-in provider | Team folder mode only: the readiness check of its subject mode (`oauth2_sub_mode`) |

Notes:

- Sessions are made at each sign-in, so no object permission can name them ahead of time: session view and delete
  stay global. The most a leaked token does with them is sign people out.
- With change only on team accounts, linking a staff member's Discord account doesn't store the Discord ID in their
  Authentik attributes (logged, and the link still works); nothing in the portal reads that attribute back.
- Grant the object permissions again for each team account, team group or controlled application added later;
  without them, a start, stop or reset fails for it.
- The readiness check "Authentik sessions visible" tests the view permission: with one account per team, it passes only
  when the token sees at least one session, so sign in to Authentik first; with several accounts per team it passes at
  once, since the portal resets no team credentials. It can't test delete.

The token can't create accounts or groups: create them in Authentik.

### Permission groups

Each role's group is its own setting, named in the table; by default it is `PORTAL_GROUP_PREFIX` plus a fixed suffix
(`web/portal/settings.py`, read by `web/core/permission_constants.py` `PERMISSION_MAP`). Create the ones you use (named
here with the default prefix):

| Group (setting) | Default | Grants |
|---|---|---|
| `PORTAL_ADMIN_GROUP` | `Portal_Admin` | Everything, including Django's admin at `/admin/` |
| `PORTAL_GOLD_GROUP` | `Portal_GoldTeam` | Gold team, and every white, orange and blue team permission: the competition and its setup, teams and their accounts, broadcasts, packets, scorecards, ticket categories |
| `PORTAL_WHITE_GROUP`, `PORTAL_WHITE_LEAD_GROUP` | `Portal_WhiteTeam`, `Portal_WhiteTeam_Lead` | White team pages (inject grading, incident reports); the lead adds the white team lead's, and needs the white team group too for the white team pages |
| `PORTAL_ORANGE_GROUP`, `PORTAL_ORANGE_LEAD_GROUP` | `Portal_OrangeTeam`, `Portal_OrangeTeam_Lead` | Orange team pages; the lead adds the orange team lead's |
| `PORTAL_RED_GROUP` | `Portal_RedTeam` | Red team findings |
| `PORTAL_TICKETING_SUPPORT_GROUP` | `Portal_Ticketing_Support` | Working tickets |
| `PORTAL_TICKETING_ADMIN_GROUP` | `Portal_Ticketing_Admin` | Ticketing support, plus ticket administration and ticket categories |
| `PORTAL_BLACK_GROUP` | `Portal_BlackTeam` | Only its Discord role |
| A team's group (below) | `TEAM_GROUP_PATTERN` | That team's blue team access: its own tickets and packets |

To use groups you already have, name each role's group directly; a name may contain spaces, and a setting left unset
or set empty keeps its default (`PORTAL_ADMIN_GROUP` included). Each role needs its own group: the portal refuses to start when two role settings name
the same one. A region whose green team works tickets and whose gold and black teams are `Gold Team` and
`Black Team`:

```
PORTAL_TICKETING_SUPPORT_GROUP="Green Team"
PORTAL_GOLD_GROUP="Gold Team"
PORTAL_BLACK_GROUP="Black Team"
```

A renamed role's default name then grants nothing. The black, white, orange, red and gold team settings also name the
groups `BLACKTEAM_ROLE_ID` and the other role settings map to Discord roles.

### Teams and team accounts

`prepare_database` creates teams 1 to `MAX_TEAMS` the first time. A new team's Authentik group is
`TEAM_GROUP_PATTERN` (`Portal_BlueTeam01` by default); after that each team keeps the group stored on it (editable in
Django's admin). An active team's group grants blue team access to its members.

`TEAM_ACCOUNT_PATTERN` says which Authentik accounts are a team's; `{number}` stands for the team's two-digit number.

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

### Teams without Authentik groups (team folder)

An Authentik that keeps team accounts in a user folder (the user's path) instead of team groups sets
`TEAM_ACCOUNT_PATH` to that folder, such as `blue-users`. Then an account in that folder whose username fully matches
one team's `TEAM_ACCOUNT_PATTERN` is that team's account and holds the team's group (its group in Django's admin, at
first the `TEAM_GROUP_PATTERN` name), though no such Authentik group exists. An account outside the folder, or in it
with a username matching no team (`bond007`), is on no team. Empty (the default), teams are their groups, as above.

For example, with accounts `team01a` to `team01i`, `team02a`, ... in the folder `blue-users`:

```
TEAM_ACCOUNT_PATH=blue-users
TEAM_ACCOUNT_PATTERN=team{number}[a-i]
TEAM_GROUP_PATTERN=Team-Blue-{number:02d}
```

- **Membership.** The group refresh (every 5 minutes) and each sign-in give each folder account its team's group, so
  blue team access, the team's tickets and its Discord role follow the folder. Only the folder does: both drop every
  team's group (each team's group in Django's admin and each `TEAM_GROUP_PATTERN` name) from the `groups` claim and
  from the account's Authentik groups first. A scope mapping that puts the team's group in the claim (such as
  `"groups": ["Team-Blue-" + user.username[-3:-1]]`) can stay, and grants nothing by itself.
- **Usernames can't be self-chosen (required).** A team comes from the folder plus a full username match, so a user
  who could rename their own account could move it into another team (`team03a` renamed `team07e`). Keep Authentik's
  System settings "Allow users to change username" (`default_user_change_username`) off, its default, and don't set
  the `goauthentik.io/user/can-change-username` attribute, which overrides it, on a folder account or its groups. The
  readiness check "Usernames fixed" fails when the setting is on or the token can't read it, or a folder account or
  one of its groups (with their parents) sets the attribute. It can't see a custom user settings flow: keep the
  brand's default one, or one whose username policy is the default's.
- **Portal usernames.** A scope mapping may give several accounts of a team one `preferred_username` (`team03a` and
  `team03b` both as `team03`). At each sign-in the portal looks the account up in the folder by its `uid` and, when
  found, uses its own Authentik username, so each account keeps its own portal account. This needs the provider's
  Subject mode at its default, "Based on the User's hashed ID" (`hashed_user_id`), which makes `sub` the user's `uid`;
  the readiness check "Sign-in subject mode" fails otherwise. Each web process remembers what it read of the folder
  for 60 seconds and stops reading at the page holding the account, so a burst of team sign-ins shares its reads; each
  account not in the folder (staff) costs one full listing per 60 seconds. An account not in the folder signs in with
  its claimed username and its claimed groups other than team groups. When Authentik can't be asked, only a sign-in
  whose claim names a staff role's or the admin group goes ahead (without team groups); every other one is refused
  with "Try again in a moment" (logged).
- **Start and stop.** A start enables each active team's folder accounts and a stop disables them, read from one
  listing of the folder (each row's path checked). Grant the token change on each team account again whenever the
  accounts are recreated (see the permissions table). Granting change on all users instead saves that, at the cost
  `THREAT_MODEL.md` accepts under "A team folder deployment may give the token change on all users": a leaked token
  could then edit any account, admins included.
- **Controlled applications.** In team folder mode a controlled application's blue team binding may also be to a
  policy whose name contains `blueteam`, such as an expression policy named `BlueTeam access`:

  ```python
  return request.user.path == "blue-users"
  ```

  A start enables that binding and a stop disables it, as with a group binding. Keep only one blue team binding: a
  start and stop toggle only one (a policy one before a group one), and the readiness check "App bindings configured"
  fails an app with more, naming the extra ones to remove. Authentik lets every user into an
  application with no enabled binding, and the recreated team accounts are enabled before the event, so disabling a
  lone blue team binding would open the app to everyone. Give each controlled application a second binding that isn't
  a blue team one, such as to the staff group: the readiness check fails an app without one enabled.
- **Not available:** the readiness fix that puts accounts in their team groups. Password resets and the packet's
  credentials work only with one account per team, as without the folder.

### Controlled applications

The Authentik applications teams get only while the competition runs (a VPN, a remote desktop gateway) are its
controlled applications, chosen on the Competition setup page (`/ops/admin/competition/setup/`, "Team access"). Each
one needs a policy binding to a group whose name contains `blueteam` (any case), such as a group holding the team
groups, or in team folder mode to a policy whose name contains it (see the team folder above); a start enables that
binding and a stop disables it. A start is refused while the list is empty, and the readiness check "App bindings
configured" fails for an application without such a binding, and in team folder mode for one without a second enabled
binding.

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
(the stop still completes), and the worker logs the failed sync once and retries it every 15 minutes.

Close Out keeps the portal's side of an event in its archive (Download Close Out Archive on the event's page): every
export, plus each ticket's comments (its Discord thread or Zulip topic as the portal recorded it), its history, and
each attachment's name, size and SHA-256 (not the file). Quotient's inject submissions and the divisor's log (the
source addresses the runners used) stay on the scoring host, where the next event's containers replace the log. Copy
`web/quotient/archive_scoring_host.py` there and run it once the event is over, before recreating its containers:

```bash
python3 archive_scoring_host.py archive <event's Compose directory> <archive directory>
python3 archive_scoring_host.py verify <archive directory>/<event>-scoring-host-<time>.tar.gz
```

It writes a tarball of the submissions, the log and a manifest of each file's SHA-256, with a `.sha256` beside it,
both readable only by you. It never replaces a file (it names them by hard link, so the archive directory's
filesystem must support hard links), and it leaves out any symlink under `submissions/`, listing it instead. Run
`verify` again on the copy wherever you keep it.

### Proxmox

With Proxmox, staff reset a box from a ticket in the box reset category (`BOX_RESET_CATEGORY_ID`, the seeded "Box
Reset / Scrub"): the VM is rolled back to a snapshot (`PROXMOX_DEFAULT_SNAPSHOT` is offered first) and started, and the
ticket is resolved. The Quotient sync also adds the team VMs Quotient doesn't score (routers, firewalls) to the box
lists.

- `PROXMOX_API_URL` is one host or several, comma-separated: a request moves on to the next host when one refuses the
  connection, and a GET also when one times out or answers 502, 503 or 504. `PROXMOX_API_TOKEN` is
  `user@realm!tokenid=secret`.
- The team VMs follow one layout (`web/core/proxmox.py`): team N's VMs are in the pool `blue-team-NN`, with VMIDs from
  `10000 + N*100` to `10000 + N*100 + 99`, named `teamNN-<host>`. Tags aren't used.
- The token reads pools, lists, rolls back and starts VMs, and reads task status. Proxmox's documentation names the
  privileges for those calls (Pool.Audit, VM.Audit, VM.Snapshot.Rollback, VM.PowerMgmt, Sys.Audit). A
  privilege-separated token holds only what both it and its user hold, so grant them to the user and the token.
- `PROXMOX_CA_FILE` trusts the cluster's own CA; `PROXMOX_VERIFY_SSL=false` turns verification off. Every host in
  `PROXMOX_API_URL` must be https.

Without Proxmox, a box reset ticket says Proxmox isn't configured and is resolved by hand.

### aweful-proxy

Each Quotient metadata sync posts the team network layout it reads from Quotient (team count, address masks, the team
DNS server and domain) to `AWEFUL_PROXY_URL/config`, with `AWEFUL_PROXY_TOKEN` as a Bearer token when set. A failed
post is only logged. Without `AWEFUL_PROXY_URL` nothing is posted. With a token the URL must be https, unless the proxy
is reached over a network nobody else can read (an in-cluster Service) and `ALLOW_HTTP_URLS` names
`AWEFUL_PROXY_URL`.

### Nextcloud

The worker gives each team a Nextcloud account through the `user_oidc` app, so team members sign in to Nextcloud
through Authentik. The account's name is the team's account username (`team01`; with several accounts per team,
`teamNN`).

- A competition start makes the active teams' accounts; opening an event remakes them; Close Out and the Discord
  `/competition cleanup-competition` delete them with their files; handing a team to a new school remakes its account.
- Set `NEXTCLOUD_URL`, a Nextcloud admin as `NEXTCLOUD_USERNAME` and `NEXTCLOUD_PASSWORD`, and
  `NEXTCLOUD_OIDC_PROVIDER_ID` to the numeric id of the Authentik provider in `user_oidc`.

It runs in the worker, so it works with or without a chat backend. Without the three settings the Nextcloud steps are
skipped.

### Email

Packets and scorecards are emailed from `DEFAULT_FROM_EMAIL`, packets with `DEFAULT_REPLY_TO_EMAIL` as Reply-To when
set. The backend is SMTP with the `EMAIL_*` settings unless `EMAIL_BACKEND` names another.
`django.core.mail.backends.console.EmailBackend` prints each message to the log instead and sends nothing; packets
carry team passwords, so use it only for testing.

### Error alerts

`DISCORD_ERROR_WEBHOOK_URL`, a Discord channel webhook (https only), gets an alert for each server error and each
error the portal logs to `wccomps.errors`; a repeat within 10 minutes, or more than 5 in a minute, is held back and
counted in the next alert. It works without the Discord bot.

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
2. Invite it to the competition server (`DISCORD_GUILD_ID`) and the volunteer server (`VOLUNTEER_GUILD_ID`), where
   staff only `/link`. Both are required; with one server, set `VOLUNTEER_GUILD_ID` to the `DISCORD_GUILD_ID` value.
   Give the bot **Administrator**: the team categories it creates deny @everyone, which takes away the bot's own access
   without it, and Discord refuses a channel permission the bot sets but doesn't hold itself. Its role must be above
   every role it assigns. Broadcasts ping the blue team role, which needs that role mentionable or the bot's Mention
   All Roles permission (Administrator includes it).
3. Create the roles and channels in [Discord settings](#discord-settings) and set their IDs (Developer Mode,
   right-click, Copy ID). An ID at 0 turns off what needs it; at startup the bot logs the required ones still at 0 and
   posts them to the ops log channel, and the readiness checks list them.

Members run `/link`, sign in to the portal, and get their roles; a team member also gets the team's role (`Team NN`) and
category (`team NN`, with `teamNN-chat` and `teamNN-voice`), which the bot creates. Besides the white and orange team
roles, these roles see every team category when they exist, found by name: `<ORG_SHORT_NAME> Observers` (read only),
`Orange Team Guest`, `Black Team Guest`, `<ORG_SHORT_NAME> Room Judge` (read only), `<ORG_SHORT_NAME> Operations Team`,
and `DISCORD_SERVER_OWNER_ROLE` when set.

### Zulip

1. Create a bot in the Zulip realm and set `ZULIP_SITE` (https only), `ZULIP_BOT_EMAIL` and `ZULIP_API_KEY`.
2. Create the streams in [Zulip settings](#zulip-settings) (one per team, each team's broadcast stream when
   `ZULIP_TEAM_BROADCAST_STREAM` is set, staff tickets when `ZULIP_STAFF_STREAM` is set, announcements and ops) and
   subscribe the bot to all of them: Zulip delivers the bot only the streams it is subscribed to. A notice for a
   missing stream fails, and the ops stream is told to create it.
3. Let the bot see members' email addresses, which it looks up for each reply. A member's email visibility (a personal
   setting; the realm sets its default for new users) must include the bot: members, or admins with an admin bot.
   Everyone isn't needed.

Each ticket gets a topic in its team's stream (`ZULIP_TEAM_STREAM`), named by its number and title, and a notice in the
staff stream; a new ticket mentions the user groups `ZULIP_TEAM_MENTION` and `ZULIP_STAFF_MENTION` when set. With
`ZULIP_STAFF_STREAM` empty there is no separate staff notice: the new ticket's post in its team's stream, mentioning
`ZULIP_STAFF_MENTION`, is the only one. Broadcasts go to the announcements stream, or to each team's broadcast stream
and topic (`ZULIP_TEAM_BROADCAST_STREAM`, by default the team's stream, and `ZULIP_TEAM_BROADCAST_TOPIC`, by default
`announcements`); a region that keeps team chat apart from tickets might set `Team {number:02d} Chat` and
`automation-messages`. Ops notices and competition status go to the ops stream. The runner long-polls Zulip, so it
needs no public address.

A message in a ticket's topic becomes a comment, and its uploads (up to 10) are stored on the ticket, only when the
sender's Zulip account email (step 3) matches, ignoring case, exactly one portal account that may write to the ticket:
staff, or a member of the ticket's team. A portal account exists from its first sign-in, with the email Authentik
gives it, so members sign in to the portal once and keep the same email in Zulip and Authentik. With one shared
account per team, that is the shared account's email, so only a member using it in Zulip can reply there; the others
reply in the portal. Anyone else is told so in the topic and nothing is saved. Tickets are opened in the portal (or
Discord), not in Zulip.

The portal trusts the realm for this (`THREAT_MODEL.md`, accepted risks): the realm must not let anyone take an email
address they don't own, and the bot sees every message in the streams it is subscribed to.

## Ticket categories

Ticketing migration 0009 seeds six categories: Service Scoring Validation, Box Reset / Scrub (the Proxmox box reset),
Scoring Service Check, Black Team Phone Consultation, Black Team Hands-on Consultation, and Other / General Issue, each
with its point cost and required fields. Edit, add or delete them at `/ops/admin/categories/` (gold team or ticketing
admin). If you replace Box Reset / Scrub, point `BOX_RESET_CATEGORY_ID` at the new category's ID.

## Settings reference

Every setting, with its default when unset and its meaning. `.env.example` has the same groups.

### Required

| Setting | Default | Meaning |
|---|---|---|
| `ORG_NAME` | empty (refused) | Your organization's name; signs packet emails |
| `ORG_SHORT_NAME` | empty (refused) | Short name; heads email subjects and scorecards, and starts the Observers, Room Judge and Operations Team role names |
| `AUTHENTIK_URL` | empty (refused) | Authentik's address, for the API; https |
| `DEFAULT_FROM_EMAIL` | empty (refused) | From address of packet and scorecard emails |
| `DJANGO_SECRET_KEY` | random per process (the servers refuse) | Signs sessions, CSRF tokens and OAuth state; keep it fixed. At least 50 characters with 5 or more distinct ones |
| `ALLOWED_HOSTS` | `localhost,127.0.0.1` | Host names the portal answers to, comma-separated |
| `BASE_URL` | `http://localhost:5000` | The portal's public address, for links in chat and email; an https one is CSRF's trusted origin |
| `AUTHENTIK_OIDC_URL` | `AUTHENTIK_URL/application/o/discord-bot/` | The sign-in provider's OpenID base; https |
| `AUTHENTIK_CLIENT_ID` | none (sign-in answers "OAuth is not configured") | The provider's client ID |
| `AUTHENTIK_SECRET` | none (sign-in fails at the token exchange) | The provider's client secret |
| `AUTHENTIK_TOKEN` | empty | The API token |
| `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` | `localhost`, `5432`, `wccomps`, `wccomps`, `wccomps` | Postgres. Compose sets `DB_HOST` and `DB_PORT` itself and creates the database with the other three; its Postgres refuses to start while `DB_PASSWORD` is empty |

### Optional

| Setting | Default | Meaning |
|---|---|---|
| `DJANGO_DEBUG` | `false` | `true` only on a developer's machine; it also lets the cookies go over plain HTTP |
| `TRUST_CF_CONNECTING_IP` | `false` | `true` when every request reaches web through Cloudflare, or a proxy that sets `CF-Connecting-IP` to the client's address ([The reverse proxy](#the-reverse-proxy)), so the registration rate limit and the app's access log use the client address |
| `WEB_PORT` | `127.0.0.1:8000` | Where Compose publishes the web app: localhost only; `8000` publishes it on every interface |
| `PORTAL_TAG` | `main` | Image tag Compose runs; a `sha-<commit>` tag pins one build |
| `GUNICORN_WORKERS` | `4` | Gunicorn workers; each holds a database connection, plus one per thread while packets are emailed, so keep workers times web replicas well under Postgres `max_connections` (120 in Compose) |
| `EMAIL_BACKEND` | `django.core.mail.backends.smtp.EmailBackend` | Django email backend |
| `EMAIL_HOST`, `EMAIL_PORT` | `localhost`, `587` | SMTP server |
| `EMAIL_USE_TLS`, `EMAIL_USE_SSL` | `true`, `false` | SMTP encryption: STARTTLS, or TLS from the start |
| `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD` | empty | SMTP login |
| `DEFAULT_REPLY_TO_EMAIL` | empty (none) | Reply-To of packet emails |
| `SERVER_EMAIL` | `DEFAULT_FROM_EMAIL` | Django's error mail sender; the portal sets no `ADMINS`, so Django sends none |
| `QUOTIENT_API_URL`, `QUOTIENT_USERNAME`, `QUOTIENT_PASSWORD` | empty (off) | Quotient; the URL must be https |
| `PROXMOX_API_URL`, `PROXMOX_API_TOKEN` | empty (off) | Proxmox hosts (each https) and token |
| `PROXMOX_CA_FILE` | empty (system CAs) | CA file for the Proxmox hosts |
| `PROXMOX_VERIFY_SSL` | `true` | Verify the Proxmox certificate |
| `PROXMOX_DEFAULT_SNAPSHOT` | `clean` | Snapshot a box reset offers first |
| `BOX_RESET_CATEGORY_ID` | `2` | The ticket category with the box reset |
| `AWEFUL_PROXY_URL`, `AWEFUL_PROXY_TOKEN` | empty (off) | aweful-proxy; with a token, the URL must be https |
| `ALLOW_HTTP_URLS` | empty (all https) | Service URL settings, comma-separated, allowed to be plain http: only for a service on a network nobody else can read, such as `AWEFUL_PROXY_URL` at an in-cluster address |
| `NEXTCLOUD_URL`, `NEXTCLOUD_USERNAME`, `NEXTCLOUD_PASSWORD` | empty (off) | Nextcloud and its admin login; the URL must be https |
| `NEXTCLOUD_OIDC_PROVIDER_ID` | `1` | The Authentik provider's id in Nextcloud's `user_oidc` app |
| `HTTPX_DEFAULT_TIMEOUT` | `10` | Seconds for outside calls: Authentik, Proxmox, Nextcloud, aweful-proxy, Zulip, the Quotient login |
| `DISCORD_WEBHOOK_TIMEOUT` | `5` | Seconds for the error alert post |
| `DISCORD_ERROR_WEBHOOK_URL` | empty (off) | Discord webhook for error alerts; must be https |
| `BOT_HEARTBEAT_DIR` | `/app/run/heartbeat` (the image creates it) | Where the worker and chat write the heartbeats `core.health` reads; for a plain run, set it to a writable directory |

### Discord settings

An ID at `0` turns off what needs it.

| Setting | Default | Meaning |
|---|---|---|
| `DISCORD_BOT_TOKEN` | empty (Discord off) | The bot's token; any value configures Discord |
| `DISCORD_GUILD_ID` | `0` | Competition server, where roles are synced |
| `VOLUNTEER_GUILD_ID` | `0` | Volunteer server, where staff only `/link`; required, so with one server set it to the `DISCORD_GUILD_ID` value |
| `DISCORD_LOG_CHANNEL_ID` | `0` | Ops log: role sync summaries, failed tasks, admin command audits, competition notices |
| `DISCORD_TICKET_QUEUE_CHANNEL_ID` | `0` | Staff ticket dashboard, one message updated within 10 seconds of a ticket change |
| `DISCORD_ANNOUNCEMENT_CHANNEL_ID` | `0` | Broadcasts to the announcements target, pinging the blue team role |
| `DISCORD_WELCOME_CHANNEL_ID` | `0` (no panel) | The ticket panel (a Create Ticket button) |
| `DISCORD_LINK_CHANNEL_ID` | `0` (no panel) | The Link Account panel; other messages posted there are deleted and answered with a link by DM |
| `BLUETEAM_ROLE_ID` | `0` | Every linked team member's role |
| `BLACKTEAM_ROLE_ID`, `WHITETEAM_ROLE_ID`, `ORANGETEAM_ROLE_ID`, `REDTEAM_ROLE_ID`, `GOLDTEAM_ROLE_ID` | `0` | The role each of the black, white, orange, red and gold team groups grants |
| `DISCORD_SERVER_OWNER_ROLE` | empty (none) | Name of a role that may talk in every team category |
| `SYNC_COMMANDS` | off (the bot syncs its slash commands when they change) | `true` forces a sync at every start |

### Zulip settings

| Setting | Default | Meaning |
|---|---|---|
| `ZULIP_SITE`, `ZULIP_BOT_EMAIL`, `ZULIP_API_KEY` | empty (Zulip off) | The realm (https) and the bot's email and API key; all three configure Zulip |
| `ZULIP_TEAM_STREAM` | `team{number:02d}` | Each team's stream, a pattern using `{number}`; tickets live here |
| `ZULIP_TEAM_BROADCAST_STREAM` | empty (`ZULIP_TEAM_STREAM`) | Each team's stream for broadcasts to teams (all teams or a range), a pattern using `{number}`; the portal refuses to start unless it gives every team from 1 to `MAX_TEAMS` its own stream, none another team's `ZULIP_TEAM_STREAM` |
| `ZULIP_TEAM_BROADCAST_TOPIC` | `announcements` | The topic of a broadcast to teams; may use `{number}`, and no other `{}` field; empty keeps the default |
| `ZULIP_STAFF_STREAM` | `tickets` | New ticket notices for staff; empty sends none (the team stream's post is the only one) |
| `ZULIP_ANNOUNCEMENTS_STREAM` | `announcements` | Broadcasts to the announcements target |
| `ZULIP_OPS_STREAM`, `ZULIP_OPS_TOPIC` | `ops`, `ops` | Ops notices and competition status |
| `ZULIP_TEAM_MENTION` | empty (none) | User group a new ticket mentions in its team's stream, a pattern using `{number}` such as `Team {number:02d}` |
| `ZULIP_STAFF_MENTION` | empty (none) | Staff user group a new ticket mentions |

### Region names

| Setting | Default | Meaning |
|---|---|---|
| `SITE_NAME` | `<ORG_SHORT_NAME> Portal` | Page titles |
| `PORTAL_GROUP_PREFIX` | `Portal_` | Start of every role group's default name |
| `PORTAL_ADMIN_GROUP` | `PORTAL_GROUP_PREFIX` plus `Admin` | The admin group |
| `PORTAL_GOLD_GROUP`, `PORTAL_WHITE_GROUP`, `PORTAL_WHITE_LEAD_GROUP`, `PORTAL_ORANGE_GROUP`, `PORTAL_ORANGE_LEAD_GROUP`, `PORTAL_RED_GROUP`, `PORTAL_BLACK_GROUP`, `PORTAL_TICKETING_ADMIN_GROUP`, `PORTAL_TICKETING_SUPPORT_GROUP` | `PORTAL_GROUP_PREFIX` plus `GoldTeam`, `WhiteTeam`, `WhiteTeam_Lead`, `OrangeTeam`, `OrangeTeam_Lead`, `RedTeam`, `BlackTeam`, `Ticketing_Admin`, `Ticketing_Support` | Each role's group; see "Permission groups" |
| `TEAM_GROUP_PATTERN` | `PORTAL_GROUP_PREFIX` plus `BlueTeam{number:02d}` | A new team's Authentik group; must use `{number}` |
| `TEAM_ACCOUNT_PATTERN` | `team{number}` | A team's Authentik accounts; must contain `{number}` |
| `TEAM_ACCOUNT_PATH` | empty (team groups) | The Authentik user folder holding team accounts, for an Authentik with no team groups; see "Teams without Authentik groups (team folder)" |
| `MAX_TEAMS` | `50` | Highest team number |

Changing a group, team account or team cap setting changes who holds which permission, and which accounts a start,
stop or reset touches.
