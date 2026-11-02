# Threat model

What the portal defends, against whom, and the rules a change must keep. Every mitigation cites the code or the test
that enforces it, by file and, where one applies, by name (`web/core/tests/test_threat_model_citations.py` fails when
a cited file or name no longer exists); a change that moves a trust boundary updates this file in the same pull request.

## Attackers and their goals

| Attacker | Starts with | Wants |
|---|---|---|
| Blue team competitor | The team's shared Authentik account, a linked Discord account | Another team's tickets, reports, scores or credentials; staff powers (scoring, resets, broadcasts); to slow or break a rival's boxes |
| Anonymous visitor | The public site | The registration form as a spam or script injection channel; any page or file without signing in |
| Discord server member | A Discord account in the server, unlinked or linked | Staff bot commands; a link to someone else's portal account; mass pings through the bot |
| Staff member over reach | One staff role (red, orange, white, ticketing) | Another role's powers: changing scores, reading team credentials, resetting accounts |
| Hostile upstream answer | A malformed or spoofed Authentik, Quotient or Proxmox response | A crash, a wrong grant, or a write made from bad data |

Competitors are adversarial and score points for disruption, so every blue team member is assumed to probe the portal
during an event.

## Assets ranked by impact

1. **Authentik service token** (`web/portal/settings.py` `AUTHENTIK_TOKEN`): sets any team's password, ends sessions,
   toggles app bindings and group membership.
2. **Proxmox API token** (`web/portal/settings.py` `PROXMOX_API_TOKEN`): rolls back and starts team VMs.
3. **Discord bot token**: posts as the bot, manages roles and channels in the event server.
4. **Team account passwords**: in Authentik, mirrored in `SchoolInfo.password` for packet emails
   (`web/core/authentik_utils.py` `_store_password`), in the bulk reset CSV (only to whoever ran the reset) and in
   packet emails.
5. **Scoring integrity**: point adjustments, inject grades, incident and red team scores that decide placings.
6. **`SECRET_KEY` and sessions**: sign sessions, CSRF tokens and OAuth state.
7. **Registrant contact details**: school contact names and email addresses.
8. **Availability during an event**: ticketing, box resets and scoring must keep working for every team.
9. **The start message**: the default VM credentials posted to every team at the start, in
   `CompetitionConfig.start_message`, in queued copies (`QueuedAnnouncement`) and in the payload of each broadcast a
   start queues, whose task id the start records in `CompetitionConfig.start_broadcasts`
   (`web/core/services/competition.py` `run_competition`). Its audit row stores only its length (`set_start_message`), and its broadcast summary omits the message (`bot/competition_actions.py` `BroadcastResult.ops_summary`).
   When the event is closed out or cancelled, or the bot's cleanup resets for the next event, the message and every
   queued announcement are cleared and each recorded broadcast is scrubbed and, if not finished, failed: a pending one
   is never sent, one being sent stops before the next team once the reset commits, and neither is retried, by the queue or by the
   admin's Retry failed tasks (`web/registration/services.py` `_reset_for_next_event`; `bot/discord_queue.py`
   `DiscordQueueProcessor._record`, `DiscordQueueProcessor._still_processing`; `web/core/admin.py`
   `DiscordTaskAdmin.retry_failed_tasks`). For broadcasts queued before starts recorded them,
   `web/core/migrations/0024_scrub_start_broadcasts.py` scrubs the finished ones and hands unfinished ones to the next reset.

## Trust boundaries and data flows

```mermaid
flowchart LR
    subgraph Internet
        B[Browser: competitors, staff, public]
        D[Discord users]
    end
    subgraph Portal
        W[Web: Django under gunicorn]
        Q[(Postgres: rows, DiscordTask queue, uploaded files)]
        T[Bot: discord.py, timers, queue processor]
    end
    A[Authentik: OIDC and admin API]
    P[Proxmox API]
    S[Quotient scoring API]
    M[SMTP]
    G[Discord API]
    B -- "1 HTTPS, session cookie, CSRF" --> W
    B -- "2 OIDC redirect" --> A
    A -- "2 callback, userinfo with groups" --> W
    D -- "3 slash commands, buttons, thread messages" --> G --> T
    W -- "4 rows, DiscordTask payloads" --> Q
    Q --> T
    W & T -- "5 service token" --> A
    W -- "6 API token, team pool only" --> P
    W & T -- "7 login session" --> S
    W -- "8 packet emails with passwords" --> M
    T -- "9 bot token" --> G
```

## Threats per boundary (STRIDE) and their mitigations

| # | Boundary | Threat | Mitigation |
|---|---|---|---|
| 1 | Browser to web | E: a role reaches a page or action it lacks | Every view carries `require_permission` (`web/core/auth_utils.py` `require_permission`) or checks itself; the views harness drives every URL as every role and fails on a wrong answer (`web/core/tests/views_harness.py`), and `web/core/tests/test_property_coverage.py` fails on a URL without a row |
| 1 | Browser to web | I: a team reads another team's ticket, report or attachment | `team_record_or_404` filters to the caller's team, so another team's record is a 404 like a missing one (`web/core/auth_utils.py` `team_record_or_404`); the ticket page and the bot's ticket thread and buttons share one rule (`web/ticketing/access.py` `ticket_open_to`) |
| 1 | Browser to web | T: a team member files a ticket as another team | The create ticket form ignores a posted team unless the view passes the teams to pick from, which it does only for gold team, admins and ticketing admins (`web/ticketing/forms.py` `CreateTicketForm`, `web/ticketing/views/create.py` `create_ticket`, `web/ticketing/tests/test_create_ticket_as_ticketing_admin.py`) |
| 1 | Browser to web | T: cross-site request forgery | `CsrfViewMiddleware` on, no `csrf_exempt` view (`web/core/tests/test_csrf_coverage.py`); views read POST fields through Django forms (`web/core/tests/test_no_raw_post.py`) |
| 1 | Browser to web | T, E: script injection through stored or reflected text | Templates and cotton attributes escape every value (`web/core/cotton.py`, `web/core/tests/test_cotton_escaping.py`, `web/core/tests/test_list_filter_escaping.py`, `web/core/tests/test_ticket_detail_escaping.py`); scripts are vendored (`web/core/tests/test_no_third_party_scripts.py`); `frame-ancestors 'none'` (`web/core/middleware.py` `SecurityHeadersMiddleware`) |
| 1 | Browser to web | T, E: script injection through a form's label, help text or error | Every field renders through the portal's row template, which escapes its label, help text and errors (Django's own marks help text safe) (`web/core/form_renderer.py` `PortalFormRenderer`, `web/core/tests/test_form_rows.py`); a forms module calls the markup functions (`format_html` or `mark_safe`) directly and with literals only, since escaping a value can't stop a `javascript:` link (`web/registration/forms.py` `RegistrationForm`, `web/core/tests/test_form_conventions.py`) |
| 1 | Browser to web | T: an uploaded file runs as a page | Every stored file goes through one model: an image or PDF is typed from its bytes and anything else never as one, an upload is read no further than its model's size limit (the bot checks Discord's reported size, `bot/cogs/ticketing.py` `TicketingCog.on_message`), and only images and PDFs show inline while everything else downloads; every file answer but an inline PDF carries a sandbox CSP (`web/core/stored_files.py` `StoredFile.from_upload`, `StoredFile.serve`, `file_response`). The admin can neither add stored files nor edit their names or types (`web/core/tests/test_stored_file_models.py`) |
| 1 | Browser to web | S: open redirect after sign-in | `next` is checked against the host at login and callback (`web/core/oauth.py` `oauth_login`, `oauth_callback`) |
| 1 | Browser to web | D: registration flood | 10 successful submissions per hour per client, an IPv4 address or an IPv6 /64, and 50 per IPv6 /48; the check and the count run under an advisory lock on the client's network (`web/registration/models.py` `RegistrationRateLimit.TIERS`, `RegistrationRateLimit.WINDOW`, `RegistrationRateLimit.held`) |
| 1 | Browser to web | S: a spoofed client address dodges the limit | `CF-Connecting-IP` counts only with `TRUST_CF_CONNECTING_IP=true`, else the connecting address does (`web/core/utils.py` `client_ip`; `web/portal/settings.py` `TRUST_CF_CONNECTING_IP`) |
| 1 | Browser to web | I: the public health check reveals the database | It answers only healthy or unhealthy and logs the error server-side (`web/core/views.py` `health_check`) |
| 1 | Browser to web | T: a red team finding scored at an access level it never claimed | The access level is one required choice, scored once from its access outcome (`web/scoring/models.py` `RedTeamScore.ACCESS_OUTCOMES`, `RedTeamScore.scored_outcomes`); a form missing it is refused, not scored as no access (`web/scoring/tests/test_red_access_level.py`) |
| 1 | Browser to web | T: an autosave or reassignment changes an orange score already submitted or approved | Each change locks the assignment row and checks the status table (`web/orange_team/models.py` `OrangeAssignmentQuerySet.locked_for`, `OrangeAssignment.STATUSES`), and a volunteer reaches only their own assignments (`web/orange_team/views.py` `assignment_save`, `assignment_submit`); concurrent edits of one check take their locks in one order (`web/orange_team/tests/test_lock_order.py`) |
| 1 | Browser to web | T: a refusal that still changes something | A refused or 4xx request writes no row, sends no mail and calls no service (the views harness rules, `web/core/tests/views_harness.py`) |
| 2 | OIDC | S: login CSRF or state replay | State is signed with a 300 s lifetime and its nonce is compared in constant time against the session (`web/core/oauth.py` `oauth_callback`) |
| 2 | OIDC | E: stale groups after a role is removed | The bot refreshes groups from Authentik every 5 minutes (`bot/cogs/authentik_groups.py` `AuthentikGroupsCog`), and a refresh aborts on an implausible answer rather than stripping everyone (`web/core/services/user_groups.py` `refresh_user_groups`); refreshes run one at a time, and none writes over a sign-in that read Authentik after it did (`web/core/services/user_groups.py` `_refresh`) |
| 2 | OIDC | E: a disabled account keeps its powers | A disabled account keeps its stored groups (they decide its team's Discord roles) but they grant nothing, on the web or the bot; a row Authentik hasn't been asked about yet counts as enabled (`web/core/models.py` `UserGroups.permission_groups`) |
| 2 | OIDC | S: a sign-in from before a credentials reset comes back in | Once Authentik has taken the new password, a reset records the time by the account's Authentik uid, in a transaction of its own, so neither an account that never signed in to the portal nor a failed unlink loses it; the callback reads it under the account lock the reset also takes, refuses a sign-in whose ID token `auth_time` is earlier, and offers a fresh sign-in. The check relies on Authentik returning `auth_time`: without it the sign-in counts from when it began at the portal, which is weaker when an Authentik session survived the reset (`web/core/models.py` `CredentialsReset`; `web/core/oauth.py` `oauth_callback`, `_signed_in_at`; `web/core/auth_utils.py` `lock_account`) |
| 2 | OIDC | S: a reused username inherits an old account | Portal identity is the Authentik `sub`, never the username (`web/core/oauth.py` `oauth_callback`) |
| 3 | Discord to bot | E: a member runs a staff command or button | Bot permissions come from the same evaluator and stored groups as the web (`web/core/auth_utils.py` `groups_grant`; `bot/permissions.py` `has_permission`), and every command, component and listener has a role row (`bot/tests/test_properties_bot.py`) |
| 3 | Discord to bot | T: a team member files a ticket as another team | A team member's ticket is for their linked team; only ticketing admins name a team (`bot/cogs/ticketing.py` `TicketingCog.create_ticket`, `bot/cogs/help_panels.py` `create_ticket`, `bot/cogs/admin_tickets.py` `AdminTicketsCog.admin_ticket_create`) |
| 3 | Discord to bot | E: someone off a team keeps its roles or commands | A team's Discord role and Blueteam follow a seat: a link on an active team whose group the account is still in (`web/team/models.py` `Team.is_held_by`, `DiscordLink.seat`; `bot/role_sync.py` `role_grants`); team commands also need the account enabled (`bot/permissions.py` `linked_team_member`, `check_blue_team`) |
| 3 | Discord to bot | S: linking someone else's portal account | Link tokens are single use, expire in 15 minutes, are bound to the browser session, and complete only after a confirming POST (`web/core/services/linking.py` `lookup_valid_link_token`, `validate_link_token`; `bot/cogs/linking.py` `TOKEN_EXPIRY_MINUTES`; `web/core/views.py` `link_callback`); a link confirmed by a session signed out since its request began is refused (`web/core/auth_utils.py` `signed_out_since`), and a disabled account can't link (`web/core/views.py` `link_callback`) |
| 3 | Discord to bot | D: mass pings through relayed text | Mentions are off by default (`bot/utils.py` `ALLOWED_MENTIONS`) |
| 4 | Web to bot queue | E: a payload makes the bot act beyond the role that queued it | Tasks are enqueued by gated views and services, and by the link flow only after its token and session checks; each payload is a typed dataclass dispatched by an exhaustive match, and a row that doesn't decode to its type (unknown type, not an object, a missing or mistyped field) fails at once without acting (`web/core/discord_tasks.py`; `web/core/models.py` `DiscordTask.typed_payload`; `bot/discord_queue.py` `DiscordQueueProcessor._process_task`) |
| 4 | Web to bot queue | T: a member's link undoes a staff edit to their team's channels | The setup a link queues only creates what is missing: when the category had no team role entry, it and each of its channels that has none get one, and it never changes an entry or adds a guest or staff role, so a deny set on a team role entry survives (`bot/discord_manager.py` `setup_team_infrastructure`); deleting is not covered (see accepted risks) |
| 4 | Web to bot queue | I: a ticket thread reaches people outside the ticket | Ticket threads are private and their members can't invite anyone (`bot/thread_creator.py` `_create_thread`); the bot adds the ticket team's active links and its assignee, never a team account (`bot/thread_creator.py` `_member_ids`), and otherwise only staff with Manage Threads see them; through the portal and the bot a ticket can be given only to someone holding `ticketing_support` (`web/ticketing/lifecycle.py` `_give`), while the Django admin can set any assignee; threads opened before this release stay invitable |
| 4 | Web to bot queue | T: a stale task acts on a newer event | A queued team teardown runs only while its event is still the open one and the competition is stopped (`web/registration/services.py` `teardown_problem`); a team handed to a new school with a Discord role or category left on it is marked `channels_need_clearing` in the hand-over's transaction, its removal queued unless one is already pending. A hand-over is an import, Add schools or registration assignment giving the team a school other than the one it held, unless members are still linked to it, and it also deletes the team's queued announcements. On School Info, a changed name is a rename, which keeps the team's school row, or moves it onto the record that already has that name; refused when another team holds it. Or it is a different school, which takes the school list lock, unlinks the team's members under the team row's lock, auditing each, marks it, and once that commits resets the team's Authentik password and ends its Authentik sessions, so the old school can't sign in to Authentik again when it succeeds; a failure is reported and names the team page's Reset Team. A team given its first school there with members still linked is unlinked and reset the same way. Any School Info edit from a stale page is refused, and Remove, Unassign and Clear are refused while members are linked, Remove and Unassign checked under the team row's lock. While the mark is set no setup reuses the role or category, no member is granted the role and broadcasts are queued rather than posted there; the mark is set when the role or category is left (a child channel whose delete fails is only logged) and unset by a delete that leaves nothing; the clear task then queues the setup if members are linked; a team failing a teardown is marked too. A removal that fails for good tells ops to delete the leftovers and run /teams recreate, whose own delete unsets the mark (`web/registration/services.py` `_clear_old_channels`, `remove_team_school`, `clear_school_list`; `web/core/services/linking.py` `unlink_teams`; `web/core/views.py` `school_info_edit`, `_report_new_school`, `school_info_remove`; `bot/discord_manager.py` `delete_team_infrastructure`, `setup_team_infrastructure`; `bot/role_sync.py` `role_grants`; `bot/utils.py` `team_chat_channel`) |
| 5 | Portal to Authentik | E: a reset or toggle reaches a non-team account | Only `team{NN}` with 1 to `MAX_TEAMS` is reset or enabled, found by exact username (`web/core/authentik_utils.py` `_team_user`; `web/core/authentik_manager.py` `AuthentikManager.find_user`) |
| 5 | Portal to Authentik | S: leaked team credentials keep working after a reset | A credentials reset ends the account's Authentik sessions, then its portal sessions and its team's Discord links, so members link again with the new password; its audit row records what each step did (`web/core/authentik_utils.py` `reset_team_credentials`, `_end_portal_sessions`) |
| 5 | Portal to Authentik | E, I: an ignored list filter acts on the wrong rows | Every list result is checked per row: sessions must belong to the target user, usernames must match exactly, a binding's target must be the app (`web/core/authentik_manager.py` `AuthentikManager.user_sessions`, `AuthentikManager.find_user`, `AuthentikManager.get_blueteam_binding`) |
| 5 | Portal to Authentik | I: Authentik's URL or answer reaches a page | Operators see `reason_for(e)`, the message alone (`web/core/authentik_manager.py` `reason_for`) |
| 5 | Portal to Authentik | T: two starts or stops, or one and a Close Out, interleave and undo each other | Web, bot and timer starts and stops hold one Postgres advisory lock for the whole run and Close Out takes it before its transaction; whoever finds it held is refused (`web/core/services/competition.py` `COMPETITION_RUN_LOCK_ID`, `run_lock`; `web/registration/services.py` `close_out_event`). The lock lives in a database session, so it needs a direct, non-pooled connection to Postgres; keepalives drop a dead holder's connection, and its lock, within about two minutes (`web/portal/settings.py` `DATABASES`) |
| 5 | Portal to Authentik | T: a malformed answer read as data | Answers must be 2xx JSON of the expected shape (`web/core/authentik_manager.py` `AuthentikManager._check`, `AuthentikManager._json`, `AuthentikManager._results`, `_holds`); property tests cover every reader (`web/core/tests/test_properties_readers.py`) |
| 6 | Portal to Proxmox | E: a ticket resets another team's VM | VMs are looked up only in the team's pool, VMID range and name prefix (`web/core/proxmox.py` `team_pool_id`, `classify_team_vm`, `ProxmoxClient.list_team_vms`); only ticket staff act (`web/ticketing/views/actions.py` `ticket_box_reset`) |
| 6 | Portal to Proxmox | T: two submissions reset one VM twice | A Postgres advisory lock per ticket admits one reset across workers and pods (`web/ticketing/views/actions.py` `_stream_box_reset`) |
| 7 | Portal to Quotient | T: a bad answer corrupts scores | Typed field validators reject the wrong shape or range (`web/quotient/client.py` `_text`, `_whole`, `_boolean`, `_fraction`); every reader parses through one helper, and the cache keeps only a parsed answer (`web/quotient/client.py` `QuotientClient._fetch`, `QuotientClient._cached`) |
| 7 | Portal to Quotient | D: an unreachable Quotient stalls ticket filing | The web ticket form and the bot's ticket autocompletes read the synced snapshot, never Quotient itself (`web/scoring/quotient_sync.py` `ticket_infrastructure`) |
| 8 | Portal to SMTP | I: a password mailed to the wrong address | Packets go to the school's contact addresses on its `SchoolInfo` (`web/team/models.py` `SchoolInfo.emails`), or to an address gold team enters for a test send; only gold team sends them, and Django admin's send actions only Discord admins reach (`web/packets/views.py` `packet_action`, `packet_resend_team`; `web/packets/admin.py` `PacketAdmin.distribute_now`, `PacketDistributionAdmin.retry_failed_emails`; `web/core/admin.py` `AuthentikAdminSite.has_permission`). A resend of failed emails claims each one before sending, so overlapping resends of failures send it once (`web/packets/services.py` `PacketDistributionService._stream_resend`) |
| 9 | Bot to Discord | I: new team passwords seen by others | A bulk reset's CSVs go only to whoever ran it: as ephemeral followups in Discord (`bot/cogs/admin_competition.py` `AdminCompetitionCog._send_reset_result`), or in the answer to their own request on the web; its ops note names teams, never passwords (`web/core/authentik_utils.py` `_bulk_reset_note`) |
| all | Logs and errors | I: tokens or OAuth codes in logs | The access log drops query strings and referers (`web/gunicorn.conf.py` `access_log_format`); httpx URL logging is off (`web/core/tests/test_httpx_logging.py`); error answers come from `web/core/errors.py`: a fixed message for 404, 405 and 500, and only the view's own stated reason for 400 and 403 |
| all | Logs and errors | T: request text forges a log line or an alert | Request text is escaped where the app logs it (`web/core/middleware.py` `_field`; `web/core/log_safety.py`), the console formatter keeps every message on one line, gunicorn escapes its access and error lines (`web/gunicorn.conf.py` `EscapingLogger`), and the Discord alert shows request text in code spans (`web/core/discord_logging.py` `_code`); property tests cover each line (`web/core/tests/test_properties_log_lines.py`) |
| all | Secrets | S: forged sessions | The web refuses to start without a real `SECRET_KEY` (`web/portal/wsgi.py`); the session cookie is HttpOnly and SameSite=Lax, and outside `DEBUG` the session and CSRF cookies are Secure with HSTS (`web/portal/settings.py` `SESSION_COOKIE_HTTPONLY`, `SESSION_COOKIE_SAMESITE`, `SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE`, `SECURE_HSTS_SECONDS`) |
| all | Repudiation | R: a staff action nobody can trace | Credential resets (with what each step did), competition start and stop, schedule edits (`/competition clear-start-time` among them), controlled-app, max-members and start-message edits, the setup page's Quotient metadata sync and group membership fix, and the cleanup reset write `AuditLog` rows naming the actor, and a teardown that runs records its own, except an opening's teardown that changed nothing and failed nowhere; resets from the team page, bulk reset and the bot post to the ops channel; a packet send records its password set as `team_password_set`. The competition timer is an actor too: its runs that finish are audited as `timer` with their attempt number, and so is giving up (`web/core/models.py` `AuditLog`; `web/core/authentik_utils.py` `reset_team_credentials`, `reset_teams_credentials`, `set_team_password`; `web/core/services/competition.py` `run_competition`, `give_up_timer_series`, `set_schedule`, `set_controlled_apps`, `set_max_members`, `set_start_message`; `web/core/admin_views/readiness.py` `sync_quotient`, `fix_group_membership`; `web/registration/services.py` `reset_for_discord_cleanup`; `bot/competition_actions.py` `reconcile_teams`; `web/core/discord_tasks.py` `LogToChannel`; `bot/utils.py` `log_to_ops_channel`) |

## Accepted risks and non-goals

- **Shared team accounts.** Members of a team share one Authentik account, so the portal cannot tell them apart and any
  member acts for the whole team (`web/team/models.py` `team_username`).
- **Authentik is the authority.** Its administrators and the portal's service account are trusted; group membership
  there is the only source of roles. The service token's own permissions bound what the portal can do.
- **Gold team holds near-admin power by design**: credential resets, app and account toggles, broadcasts and packet
  passwords. Only `WCComps_Discord_Admin` reaches Django admin.
- **Upstream truth.** Quotient's scores and Proxmox's VM state are validated for shape, not for honesty.
- **Cloudflare's client address is trusted only when configured.** A deployment behind Cloudflare sets
  `TRUST_CF_CONNECTING_IP=true`; the self-hosting `docker-compose.yml` leaves it off, so its limits key on the
  connecting address.
- **Inline scripts are allowed by the CSP**, so script injection defence rests on escaping, which the escaping tests
  guard.
- **Team passwords are deliberately readable by admins and gold team**: packet emails need them in clear text.
- **A link restores deleted team channel setup.** Deleting the category's team role entry, the role or a team
  channel is undone by any member's link; to lock a team out, set a deny on the entry and never delete it.
- **Roles change within the 5-minute refresh**, not instantly; sessions last 12 hours from the last request.
- **Role sync leaves a not-yet-refreshed link alone**: a link whose account Authentik hasn't been asked about yet keeps
  its Discord roles until a refresh or a sign-in reads the account (`bot/role_sync.py` `role_grants`).
- **A packet send's password set skips the reset protections**: it changes the password but leaves sessions,
  sign-ins from before it and Discord links alone, by design (`web/core/authentik_utils.py` `set_team_password`).
- **Clock skew at a reset**: a sign-in within 2 seconds before a credentials reset counts as after it, since
  `auth_time` is whole seconds and the two clocks differ (`web/core/oauth.py` `RESET_CLOCK_MARGIN`).
- **Out of scope:** the hosting cluster, Authentik's own security, Discord's platform, and denial of service at
  network scale.

## Build and deploy path

1. A change lands on `main` through a pull request whose CI passes: ruff format and lint, djlint, mypy, the migrations
   check and the test suite (`.github/workflows/tests.yml`).
2. Dependencies come from `uv.lock`, installed `--frozen` (`Dockerfile`); updates arrive only as Dependabot pull
   requests (`.github/dependabot.yml`).
3. CI on `main` builds one image for web and bot and pushes it to GHCR as `sha-<commit>` (`.github/workflows/ci.yml`).
   Third-party actions are pinned to commit SHAs and base images to digests (`web/core/tests/test_python_pin.py`), and
   the image runs as uid 1001, not root (`Dockerfile`). Each web container migrates under a Postgres advisory lock, so
   replicas take turns (`web/core/management/commands/prepare_database.py` `Command.handle`).
4. A separate private GitOps repository pins that tag; production changes only when a person merges the tag bump there.
   Production secrets live there, never in this repository.

## Owner and review trigger

The repository maintainers own this file. Update it in the same pull request when a change:

- adds or changes an entry point: a URL, a bot command, component or listener, a DiscordTask payload, an inbound
  callback;
- adds a role, changes `PERMISSION_MAP`, or changes who may reach a service-token action;
- adds an outside service, credential or secret, or changes where one is stored, logged or shown;
- changes how files, uploads or user text are stored or rendered;
- changes the build, the dependency policy or the deploy path.

Review it in full before each event, alongside the readiness checks.
