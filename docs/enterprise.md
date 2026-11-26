# Enterprise guide: the team server

This guide covers running RunLedger for a team: who can do what, how API keys are issued and retired,
what the audit log records, budgets and alerts, exports, HTTPS, and what the server stores. For the
installation steps, use [self-hosting.md](self-hosting.md). For every endpoint, use [api.md](api.md).

Contents:

- [Model: teams, keys and roles](#model-teams-keys-and-roles)
- [API key lifecycle](#api-key-lifecycle)
- [SCIM provisioning](#scim-provisioning)
- [Audit log](#audit-log)
- [Budgets and alerts](#budgets-and-alerts)
- [Exports and the compliance report](#exports-and-the-compliance-report)
- [Sign-in and sessions](#sign-in-and-sessions)
- [HTTPS, proxies and cookies](#https-proxies-and-cookies)
- [Rate limiting](#rate-limiting)
- [Webhooks](#webhooks)
- [Data stored and retention](#data-stored-and-retention)
- [What RunLedger does not do](#what-runledger-does-not-do)

## SCIM provisioning

For Okta or Microsoft Entra provisioning:

1. Create a team-specific credential using admin `POST /api/scim/token`, or
   `runledger scim-token create --team NAME --db runledger.db`. Store the returned token
   securely: only its hash and prefix remain on the server. Creating another rotates it.
2. Configure an Okta SCIM 2.0 application or Entra enterprise application's automatic
   provisioning with tenant/base URL `https://<host>/scim/v2` and bearer token authentication.
   Use HTTPS. API keys and dashboard cookies cannot authenticate SCIM requests.
3. Map IdP email/UPN to `userName` (a valid email), full name to `displayName` or
   `name.formatted`, enabled status to `active`, and IdP object ID to `externalId`.
   Match existing users by `userName` using the `eq` filter. Optionally map one role
   as `roles[0].value`: admin/member/viewer, default member.
4. Assign a test user, provision it, disable it, then re-enable it before rolling out.
   Three fixed groups named admin/member/viewer can set membership roles; do not enable
   group creation/deletion or password synchronization. See [SCIM API](api.md#scim-provisioning).

Deprovisioning with `active:false` blocks personal keys and deletes sessions for that team
without revoking keys or disabling the server-wide user. Re-enabling restores keys, not sessions.
Deleting a SCIM user removes membership and revokes its team personal keys. Users belonging to
other teams retain access there. **Email and name are global attributes**: provisioning cannot
change them if the user belongs to another team. Adding an existing user keeps their name.
Okta given/family name parts are combined when no formatted name is supplied. `externalId`, role
and active status are team-local. The last working admin cannot be deactivated, demoted or deleted.
`runledger user list` shows a SCIM-deactivated membership as `deactivated` (a server-wide disable shows `disabled`).

Revoke provisioning access with `DELETE /api/scim/token` or the local `scim-token revoke` command.
Token administration and SCIM changes appear in the audit log. Automated updates use actor `scim`.

## Model: teams, keys and roles

- A **team** is the unit of isolation. Teams never see each other's runs, approvals, keys, settings
  or audit events.
- Access is by **API key**. Each key belongs to one team and has one **role**. A key may belong
  to a server-wide **user**, identified by a unique, lowercase email, with a membership in that team.
  Users may join several teams. Keys of kind `service` cannot have an owner; personal keys may also
  be unowned, and existing keys stay unowned.
- Use one key per person, or per system (for example a CI job), with the least role that works.
- Pending approvals created before requester-key attribution was added cannot prove an independent
  approver. For safe separation of duties, agents must submit a fresh request after upgrading.

| Role | Can do |
| --- | --- |
| **viewer** | Read everything a team member can read: runs, receipts, statistics, approvals, and team settings with webhook URLs masked. Manage dashboard sessions started with this key. |
| **member** | Everything a viewer can do, plus push runs and request approvals. |
| **admin** | Everything a member can do, plus decide approvals, change team settings, manage keys, read the audit log, set budgets and download exports. An admin cannot decide an approval created with the same key. |

Roles are ranked: viewer < member < admin. A key's role cannot be changed in place. To change it,
create a key with the new role and revoke the old one.

Access matrix (HTTP endpoints; see [api.md](api.md)):

| Action | viewer | member | admin |
| --- | --- | --- | --- |
| Read runs, receipts, statistics, approvals | yes | yes | yes |
| Read team settings (webhook URLs masked unless admin) | yes | yes | yes |
| Push runs (`POST /api/runs`) | no | yes | yes |
| Request approvals (`POST /api/approvals`) | no | yes | yes |
| Decide approvals (`POST /api/approvals/{id}/decision`) | no | no | yes, with a different key from requester |
| Change team settings (`PUT /api/team/settings`) | no | no | yes |
| List, create, rotate and revoke keys (`/api/keys`) | no | no | yes |
| List and revoke dashboard sessions (`/sessions`) | this key | this key | this team |
| Read the audit log (`GET /api/audit`) | no | no | yes |
| Read budgets (`GET /api/budgets`, `/api/budgets/status`) | yes | yes | yes |
| Set budgets (`PUT /api/budgets`) | no | no | yes |
| Download exports (`/api/export/...`) | no | no | yes |

The last three rows describe this release's budget and export endpoints. The budget reads are open to
every role so that people can see the team's spend; only admins change it.

The command-line key and team commands (`runledger team create`, `runledger key ...`) work on the
database file directly and do not check roles. Whoever can read and write the database file can
create an admin key. Protect the file as you would protect the keys themselves.

### People and offboarding

```bash
runledger user add --team-id 1 --email ana@example.com --name Ana --role admin
runledger user list --team-id 1
runledger key create --team-id 1 --label laptop-ana --role admin --user ana@example.com
runledger user role --team-id 1 --email ana@example.com --role member
runledger user remove --team-id 1 --email ana@example.com
runledger user disable --email ana@example.com
runledger user enable --email ana@example.com
```

All these local commands accept `--db FILE`. Adding an email creates a user if needed; an existing
user is reused across teams. Display names are optional. There are no passwords or interactive
account registration: people sign in with their personal key or a configured OIDC or SAML provider.

### OIDC single sign-on

Create an OIDC web application at your identity provider, enable Authorization Code and PKCE
S256, and register `https://ledger.example.com/auth/oidc/work/callback` as its redirect URI.
Use RS256 ID tokens. Provision people and team memberships with the commands above first:
SSO never creates an account. An enabled user with no team membership cannot sign in.

Store a server-wide JSON configuration outside the repository:

```json
{
  "public_url": "https://ledger.example.com",
  "disable_key_sign_in": true,
  "providers": [{
    "id": "work",
    "name": "Company SSO",
    "issuer": "https://company.okta.com/oauth2/default",
    "client_id": "your-web-client-id",
    "client_secret_env": "RUNLEDGER_WORK_SECRET",
    "allowed_email_domains": ["example.com"],
    "email_claim": "email",
    "require_email_verified": true
  }]
}
```

Start with `runledger serve --oidc-config /etc/runledger/oidc.json` (equivalently,
`RUNLEDGER_OIDC_CONFIG`). Multiple providers are supported. `client_secret` may replace
`client_secret_env`; prefer the environment variable and restrict config file permissions.
Set `public_url` to the externally visible origin, including HTTPS. Without it the existing
`RUNLEDGER_PUBLIC_URL` or bound-server URL is used. When no public URL is supplied, the server
derives one from its bound address and actual port (wildcard binds become loopback addresses).
This is suitable for local use; configure an explicit URL behind a proxy or for remote users.
An unusable base URL fails at startup when OIDC is enabled, before accepting sign-in requests.
HTTP is allowed only for loopback testing.

- **Okta:** use the exact authorization server issuer (for example the `/oauth2/default`
  issuer above), grant the web app `openid email profile`, and allow assigned users.
- **Entra ID:** use the tenant-specific issuer
  `https://login.microsoftonline.com/<tenant>/v2.0`, never `/common`. If needed, use
  `"email_claim": "preferred_username"`. Entra may omit `email_verified`; the secure default
  rejects that. Only set `"require_email_verified": false` after ensuring that your tenant
  controls and verifies those usernames and app assignments.
- **Google Workspace:** use issuer `https://accounts.google.com` and add
  `"hosted_domain": "example.com"`. This checks the signed `hd` claim, not merely the email
  suffix. Also restrict the OAuth app to your organization.

Email lookup is case-insensitive. First sign-in binds the user's issuer and subject; another
identity cannot take over that email. A different issuer is also rejected once bound. People
with several memberships choose a team before receiving a session. Sessions have no API key;
membership roles and disabled status are checked on every request. Setting
`"disable_key_sign_in": true` in either the OIDC or SAML config disables dashboard key sign-in
without disabling API/CLI keys. Failed sign-ins count toward the address failure limit
and are audited only in the resolved user's membership teams, without tokens or supplied email
addresses. Unresolved identities and invalid states or tokens are not written to team audit logs.

The short-lived pre-auth cookie uses SameSite=Lax for the cross-site callback. The final
session cookie remains HttpOnly, SameSite=Strict (and Secure for HTTPS). The callback returns
a 200 continuation page rather than a redirect chain so the Strict cookie reaches the dashboard.

### SAML 2.0 single sign-on

Install the optional server and SAML dependencies with `pip install 'runledger-ai[server,saml]'`
(from a clone, `pip install '.[server,saml]'`). Configure your IdP for SP-initiated SAML 2.0: use HTTP-Redirect for
the AuthnRequest and HTTP-POST for the response. In this example, import the SP metadata from
`https://ledger.example.com/auth/saml/work/metadata` or register that URL as the SP entity ID
and `https://ledger.example.com/auth/saml/work/acs` as the assertion consumer service URL.
Configure the IdP to sign the assertion or response with a current SHA-2 algorithm and supply
its trusted signing certificate as PEM. AuthnRequests and SP metadata are unsigned; encrypted
assertions, unsolicited IdP-initiated login and SAML single logout are not supported.

Store a server-wide JSON configuration outside the repository:

```json
{
  "public_url": "https://ledger.example.com",
  "disable_key_sign_in": true,
  "providers": [{
    "id": "work",
    "name": "Company SAML",
    "idp_entity_id": "https://sso.example.com/metadata",
    "idp_sso_url": "https://sso.example.com/saml/login",
    "idp_cert": "company-signing.pem",
    "email_attribute": "email",
    "name_attribute": "displayName"
  }]
}
```

Start with `runledger serve --saml-config /etc/runledger/saml.json` (or
`RUNLEDGER_SAML_CONFIG`). The certificate path may be absolute or relative to the config file.
Set `public_url` to the externally visible HTTPS URL; if omitted, SAML uses the dashboard's
public URL (including any OIDC override), or the bound address for local use.
HTTP is only allowed on loopback for testing; the browser must allow a Secure cookie on the
loopback origin. Multiple providers are supported; set `sp_entity_id` if the IdP requires an
entity ID different from the provider's metadata URL. OIDC can remain enabled in parallel.

`disable_key_sign_in` defaults to `false`. Set it to `true` to remove the dashboard key form
and reject key sign-in at `POST /session` and `/?key=...`, even without OIDC configured. The SAML
config must still define at least one provider. This setting does not disable API or CLI keys.

Pre-provision each person and their team memberships; SAML does not create users or grant roles.
`email_attribute` defaults to `email` and must be a single, IdP-controlled email value.
The IdP must provide a stable non-transient NameID, or you can set `subject_attribute` to the
name of a stable, unique, signed attribute. The account is bound to the IdP entity ID and
subject separately from its OIDC binding. If `name_attribute` is configured, its value fills
an empty display name on first SAML sign-in; it does not overwrite an administrator-set name.
People with multiple teams choose one before receiving a session.

The short-lived pre-auth cookie is host-bound, Secure, and SameSite=None so the IdP can POST
back to the assertion consumer service. The dashboard session remains HttpOnly and
SameSite=Strict, and is Secure on HTTPS. Pending logins are single-use and do not survive a
server restart.

Personal keys work only while the user is enabled and is a member of the key's team. The effective
role is the weaker of the key role and membership role, checked on every API and dashboard request.
New keys cannot exceed their owner's membership role. Lowering a membership role caps existing
keys immediately, including dashboard sessions.

For team offboarding, **remove the membership**: this revokes that person's keys in the team and
deletes their team sessions, without affecting other teams. Re-adding them does not revive revoked
keys. For server-wide offboarding, **disable the user** (CLI only): their keys stop working everywhere
and their sessions are deleted. Enabling them restores unrevoked keys, not deleted sessions.
Membership changes, disable/enable and key changes are audited. Removing or demoting a member,
disabling a user, or revoking a key is refused if it would leave a team without a working admin key.
Keep a protected unowned admin key for recovery. Disabling a user does not disable service keys.

Admins can also manage members through `/api/members`; see [api.md](api.md#members).

## API key lifecycle

A key has an id, a label (1 to 100 characters, no control characters), a role, a kind, a prefix, and
creation, last-use, expiry and revocation times. The secret looks like `rl_` followed by 40 URL-safe
characters. The server stores only the SHA-256 hash of the secret and the first 8 characters (the
prefix) for display. The secret is shown once, when the key is created or rotated.

**Create.** The first admin key is created with the team, labelled `initial`:

```bash
runledger team create myteam --db runledger.db      # prints the team id and its first (admin) key
```

Further keys, from the command line (no server needed) or from the API by an admin:

```bash
runledger key create --team-id 1 --label "laptop-ana" --role member --db runledger.db
```

```http
POST /api/keys
Authorization: Bearer <admin key>

{"label": "ci-nightly", "role": "member"}
```

**List.** `runledger key list --team-id 1` or `GET /api/keys` shows every key of the team, oldest
first, with its prefix, status and last use. Secrets are never shown. The last-use time is updated at
most once a minute.

**Rotate.** Replaces a key's secret. The id, label, role and kind stay the same, and the old secret
stops working at once. Dashboard sessions that were started with the key end too. Use this when a key
may have leaked, or on a schedule. Rotation also renews the key's lifetime (`--expires-in-days N`, else
its previous lifetime, held to the team's cap), which is how an expired key is brought back. A revoked
key cannot be rotated.

```bash
runledger key rotate <key id> --db runledger.db
```

```http
POST /api/keys/<key id>/rotate
```

**Kinds.** A `personal` key (the default) belongs to one person and may sign in to the dashboard. A
`service` key is for CI or another system: it works on the API only, and the sign-in form and
`/?key=` links refuse it. Create one with `--kind service` or `"kind": "service"`. Keys from before
kinds existed are personal.

**Expiry.** A key may carry an `expires_at`. After it, the key is refused with `401 key_expired`
(not counted as a failed guess) and its dashboard sessions stop working; a session never outlives its
key. An expired key is not revoked: rotating it gives it a new secret and a new lifetime. Ask for a
lifetime with `--expires-in-days N` or `"expires_in_days": N` (1 to 3650). Keys from before expiry
existed never expire until a team cap applies to them.

**Team key policy.** An admin sets, in `PUT /api/team/settings` or on the dashboard's settings page:

| Setting | Meaning |
| --- | --- |
| `key_max_ttl_days` | Longest lifetime of a personal key, and the default for a new one. `null`: no limit. |
| `service_key_max_ttl_days` | The same for service keys, so CI keys can have their own rotation period. |
| `key_expiry_warn_days` | Days before expiry that a key is warned about (default 14; 0 turns warnings off). |

A key requested for longer than its cap is refused. A new or lower cap also shortens existing keys of
that kind that would outlive it, to that many days from now, so nothing stops working at once.

**Expiry warnings.** Inside the warning window, every API response to the key carries
`X-RunLedger-Key-Expires: <expires_at>`, `runledger push` prints a warning, `/api/me` returns
`"expires_soon": true`, and the dashboard shows a banner to a person signed in with that key. The
running server also checks hourly and, once per key lifetime, records a `key.expiring` audit event and
sends the team's Slack and generic webhooks a message (`{"type": "key_expiring", ...}` with the key's
id, label, role, kind, prefix and `expires_at`, never its secret).

**Revoke.** Ends a key permanently. A revoked key cannot be used again or rotated, and its dashboard
sessions end at once. Revoked keys stay in the list, marked with their revocation time. The team's
last active admin key cannot be revoked: create or rotate another admin key first.

```bash
runledger key revoke <key id> --db runledger.db
```

```http
POST /api/keys/<key id>/revoke
```

Keys do not expire on their own. There is no expiry field, so schedule rotation or revocation
yourself.

Every key change is written to the audit log. Changes made with the command line are recorded with
the actor `cli`, and changes made with an API key are recorded with that key's label and prefix.

## Audit log

The audit log records changes and sign-ins. Each event has an id, a time (UTC), an actor, an action, a
target and details. Details never include a key secret or a webhook URL.

| Action | Recorded when | Target | Details |
| --- | --- | --- | --- |
| `key.create` | A key is created, including the team's first key | Key id | label, role, prefix, kind, expires_at |
| `key.rotate` | A key's secret is replaced | Key id | label, role, prefix (of the new secret), expires_at |
| `key.expiring` | A key enters its team's warning window (once per lifetime; actor `system`) | Key id | label, kind, prefix, expires_at |
| `key.revoke` | A key is revoked | Key id | label, role, prefix |
| `run.push` | A new receipt is stored (identical retries are not new writes) | Run id | agent, project |
| `run.supersede` | A session is pushed again and replaces the stored run | Run id | revision, replaced_revision, agent, project |
| `approval.decide` | An approval is approved or denied | Approval id | status |
| `team.settings` | Team settings change | `settings` | the names of the fields changed, and the values of `approval_ttl_s`, `attribution` and the key policy fields that changed, plus `keys_shortened` when a new cap shortened keys. Never the URLs. |
| `auth.sign_in` | A dashboard sign-in with a key succeeds | Key id | role, and the client address (`ip`) |
| `auth.sign_out` | The caller signs out of the dashboard | Session id | — |
| `auth.session_revoked` | The caller revokes a dashboard session | Session id | — |
| `budget.update` | This release: the team's budget changes | `budget` | monthly_usd, per_user_monthly_usd, alert_thresholds |
| `budget.alert` | This release: a threshold fires | Scope (`team` or `user:<name>`) | month, threshold, spend_usd, limit_usd, pct |
| `export.runs`, `export.audit`, `export.report` | This release: an admin downloads an export (GET only, not HEAD) | `null` | days, rows |

Actors look like `initial (rl_Xk3vN)` for an API key (label and prefix), `dashboard:<label>` for a
dashboard session, `cli` for command-line changes, and `system` for budget alerts.

Not recorded: approval requests (only their decisions are), failed sign-in attempts (they count toward
the rate limit instead), guard decisions (they stay in the project's `guard.log`), and webhook
deliveries.

Read the log in pages, newest first:

```http
GET /api/audit?limit=50
GET /api/audit?limit=50&before=1042            # events with an id below 1042
```

This release adds an `action` filter that matches a prefix, so `action=key.` returns all key
changes:

```http
GET /api/audit?action=key.
```

## Budgets and alerts

*This release.* A budget is a monthly spending limit for the team, and optionally one for each
developer. Alerts tell admins and channels when spending reaches set percentages. Budgets do not stop
runs or block the guard; they only alert.

- **Spend.** The sum of the run cost estimates (list prices, see
  [agents.md](agents.md#costs-across-agents)) for runs that started in the current calendar month in
  UTC. A run with no start time counts in the month it was pushed.
- **Unknown costs.** A run whose cost is unknown (the agent recorded no usage, or no model had a
  price) adds nothing to the month's spend, and a run whose cost is only partly priced counts with the
  part that is known. A month of unknown runs therefore reads `$0` in the API, never a made-up figure,
  and the dashboard shows `n/a` for it; a month with partly priced runs reads `≥ $X` there, and the
  note next to the figure names the runs it leaves out. The status and the alert carry the run counts
  behind the number, and say the figure is a lower bound when some run is unpriced or only partly
  priced.
- **Limits.** `monthly_usd` for the team and `per_user_monthly_usd` for each developer. `null` or `0`
  means no limit.
- **Attribution.** The developer a run counts against is the run's `user`, which follows the team's
  `attribution` setting: `client` (default) uses the name the client sent, and `key` uses the pushing
  key's label so a per-user budget cannot be dodged by editing the receipt. See
  [api.md](api.md#get-apiteamsettings).
- **Thresholds.** `alert_thresholds`: one to three whole percentages from 1 to 500, ascending. The
  default is `[50, 80, 100]`.
- **When alerts fire.** After every push. Each threshold fires once per scope per month: once for the
  team, and once for each developer, so a repeated push never repeats an alert. Alerts of earlier
  months are deleted by retention; the alert of the current month is kept by its month, so pruning can
  never make the same threshold fire twice in a month.
- **Where alerts go.** The team's Slack and generic webhook URLs (the same settings as approvals). Each
  alert is recorded as a `budget.alert` audit event.

```http
PUT /api/budgets
Authorization: Bearer <admin key>

{"monthly_usd": 500, "per_user_monthly_usd": 60, "alert_thresholds": [50, 80, 100]}
```

The body replaces the whole budget. A missing amount means no limit, and missing thresholds mean the
defaults. Unknown fields are rejected.

`GET /api/budgets/status` shows this month's spend against the budget:

```json
{
  "month": "2026-10",
  "spend_usd": 412.37,
  "monthly_usd": 500.0,
  "pct": 82.5,
  "runs": 30,
  "priced_runs": 28,
  "unpriced_runs": 2,
  "partial_runs": 1,
  "cost_is_lower_bound": true,
  "per_user": [{"user": "ana@example.com", "spend_usd": 58.1, "pct": 96.8,
                "runs": 5, "priced_runs": 5, "unpriced_runs": 0, "partial_runs": 0}]
}
```

A budget alert sent to a generic webhook has this shape. The lower-bound fields are added only when
some of the month's runs have no complete price:

```json
{"type": "budget_alert", "team": "myteam", "month": "2026-10", "scope": "team", "user": null,
 "threshold": 80, "spend_usd": 412.37, "limit_usd": 500.0, "pct": 82.47,
 "cost_is_lower_bound": true, "runs": 30, "unpriced_runs": 2, "partial_runs": 1,
 "note": "lower bound: 2 of 30 runs unpriced"}
```

The Slack message says the same: `RunLedger budget: team spend $412.37 is 82% of $500.00 for 2026-10
(lower bound: 2 of 30 runs unpriced)`.

## Exports and the compliance report

*This release.* Admins can download three files for a look-back window of `days` days (default 30, from 1
to 3650, counted back from now in UTC):

| Endpoint | Format | Contents |
| --- | --- | --- |
| `GET /api/export/runs.csv?days=N` | CSV | One row per run that started (or was pushed) in the window, oldest first: id, started_at, user, project, agent, models, steps, tokens, files_changed, cost_usd, risk_score, risk_level, title, quality_score, quality_grade, ai_verdict, est_savings_usd, cost_partial. Never the receipt JSON or HTML. |
| `GET /api/export/audit.csv?days=N` | CSV | Every audit event in the window, oldest first: id, at, actor, action, target, details_json. |
| `GET /api/export/report.html?days=N` | HTML | A report for the window: totals, runs by risk level, top risk codes, high-risk runs, approvals decided, key lifecycle events, this month's budget status, and a note on the method. Each long table shows at most 200 rows. |

The CSV files are UTF-8 with a byte-order mark, and a cell that a spreadsheet could read as a formula
is written as text. The report contains no scripts and no outside resources. Each download is recorded in
the audit log.

A run whose cost is unknown leaves the `cost_usd` cell empty rather than writing `0`; `quality_score`
and `est_savings_usd` are empty in the same way. `cost_partial` is `true` when the run's `cost_usd` is a lower bound
because some of its tokens had no price. The report states the window's cost as `n/a` when no
run in it could be priced, and as a lower bound (`≥ $X`, with the number of runs it leaves out) when
some runs are unpriced or only partly priced; the month's developer rows follow the same rule, so a
developer whose runs are only partly priced reads `≥ $X` too.

Each endpoint also answers `HEAD`, with the same headers and no body. Exports are admin-only.

**What the report is, and is not.** The report and exports help you keep records of what your team's
agents did, what they cost, which risks were flagged, and who decided which approvals. They are
evidence you can file. **They do not make you compliant with SOC 2, the EU AI Act or any other
framework by themselves.** Compliance depends on your controls, policies and processes, on who has
access, and on what you choose to record and keep. Receipts contain prompts and command text, so treat
exports as confidential.

## Sign-in and sessions

- Open `https://your-server/` and sign in with your team API key in the form. The key is sent in the
  request body and never appears in the address bar, browser history or proxy logs.
- The legacy link form `https://your-server/?key=YOUR_KEY` still works for links printed by
  `runledger serve` and `team create`. It redirects so the key leaves the address bar, and it logs one
  deprecation warning. If the browser already holds a session for a **different** team, the link shows
  a confirmation page instead of switching: one click is always required, so a link cannot silently
  sign you into someone else's team.
- The cookie is `rl_session`: HttpOnly, SameSite=Strict, and valid for 12 hours. It is Secure when the
  server is behind HTTPS (see below).
- A session carries the role of its key. It ends after 12 hours, at its key's `expires_at` if that is
  sooner, or at once when its key is revoked or rotated. Service keys cannot sign in. Each key keeps at most 20 sessions; signing in past that cap drops that key's oldest
  session and leaves other keys alone.
- The server keeps sessions in the team database as hashes of their tokens, so restarting it does
  not sign anyone out.
- **Sign out** ends the current session without changing its API key. The **Sessions** tab shows
  active sessions and lets you revoke them. Admins see the entire team's sessions; members and
  viewers see only sessions started with their key.
- An operator may set an inactivity timeout in seconds with `RUNLEDGER_SESSION_IDLE_TIMEOUT` or
  `runledger serve --session-idle-timeout`. The default `0` disables idle expiry. Dashboard
  background polling does not count as activity, and the 12-hour absolute limit still applies.
- Changes made from the dashboard must send the header `X-Requested-With: runledger`. A cross-site form
  cannot set it, so a forged request is refused with `csrf_required`. Requests that use an API key in
  the `Authorization` header do not need it.
- The sign-in POST itself is checked too: a request a browser marks as coming from another origin
  (`Sec-Fetch-Site: cross-site` or `same-site`, which covers another port or a subdomain; or an
  `Origin` header that is not this server's own origin, `null` included) is refused with
  `403 cross_site` before the key is read. No cookie is set, so a page that auto-submits a form to
  `POST /session` with someone else's key cannot replace the session you are signed in with, and the
  server writes a warning line that never contains the key. This matters because the cookie is
  `SameSite=Strict`: a cross-site form arrives without your session, so the server could not show the
  switch confirmation below. The two headers are checked independently, so `Sec-Fetch-Site:
  same-origin` never excuses a concrete `Origin` that names another site. The fetch metadata only
  excuses an absent `Origin` and the opaque `null` one, which is what Chromium sends for the
  dashboard's own form because every response carries `Referrer-Policy: no-referrer`. A request with
  neither header (curl, a script) is not a browser and signs in as usual.

## HTTPS, proxies and cookies

Run the server only over HTTPS. Keys travel in the `Authorization` header and in the sign-in step.
Three ways to get HTTPS:

1. **A reverse proxy** (recommended): Caddy, which gets certificates automatically (the Docker Compose
   setup in [self-hosting.md](self-hosting.md)), or nginx with a certificate you manage. The proxy
   must forward `X-Forwarded-Proto`, and the server must be reachable only through the proxy.
2. **The server's own TLS:** `runledger serve --tls-cert FILE --tls-key FILE`. Both files are PEM,
   and the server accepts TLS 1.2 and later. Give both options together; the command refuses to start
   if a file is missing or cannot be loaded.
3. **Your own load balancer,** with the same rule: only the proxy can reach the server.

Cookie and header flags:

- `--secure-cookies`, or `RUNLEDGER_SECURE_COOKIES=1`: sets the `Secure` flag on the session cookie,
  and sends `Strict-Transport-Security` (HSTS, one year).
- `--trust-proxy`: honours `X-Forwarded-Proto: https` from a reverse proxy, so the cookie is `Secure`
  and HSTS is sent when the browser used HTTPS. With no trusted proxies configured, the header is
  honoured from any address, so use it only when the proxy is the only way in. With trusted proxies
  configured, the header counts only from those addresses.
- `--trusted-proxy CIDR` (repeatable; also `RUNLEDGER_TRUSTED_PROXIES`, comma-separated): *this release.*
  The addresses or ranges of your reverse proxies, for example `10.0.0.0/8` or `203.0.113.5`. The server
  prints `Trusting X-Forwarded-For from: ...` when it starts, and a bad entry stops it before the database
  is opened. From a trusted address, the client is the right-most `X-Forwarded-For` address that is not
  itself trusted, so a client cannot choose its own address by adding entries on the left. From any other
  address the header is ignored. The address is used for the failed-sign-in limit and in the audit log.

Every response carries `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`,
`Referrer-Policy: no-referrer` and `X-Frame-Options: DENY`. The dashboard uses a Content Security
Policy with a per-page nonce, and receipts are served with a stricter policy that blocks scripts and
external resources.

## Rate limiting

Each client address may have up to 20 failed credentials in any five-minute window. A failed credential
is a rejected API key or a failed dashboard sign-in. Once an address is over the limit, its requests
get `429 rate_limited` with a `Retry-After` header, until enough failures age out. `/health` is not
limited. Requests that carry no credentials do not count, and neither do expired dashboard cookies.

The address is the one the TCP connection comes from. Behind a reverse proxy, that address is the
proxy's, so all of your users share one counter: a burst of bad keys from anyone can lock out everyone
behind that proxy for up to five minutes. Set `--trusted-proxy` (above) so that the limit counts each
real client. The counter is held in memory, so a restart clears it.

## Webhooks

Team settings can hold a Slack incoming-webhook URL and a generic webhook URL. Both are used for
approval requests, for budget alerts in this release, and for API keys about to expire.

- URLs must use public `https://` destinations. Internal, private, loopback, link-local and
  non-public DNS answers are blocked, including when public and private answers are mixed.
- For local testing only, set `RUNLEDGER_ALLOW_LOOPBACK_WEBHOOKS=1` to permit `http://127.0.0.1`
  or `http://localhost`. Never enable this exception on a shared or Internet-accessible server.
- DNS addresses are verified on delivery and the socket connects to the verified address
  (mitigating DNS rebinding). Redirects and environment proxy settings are not followed.
- Outbound webhooks need directly routable public IPv4 or public unicast IPv6. NAT64/DNS64-only
  and 6to4 egress are intentionally unsupported by this stricter destination policy.
- Admins see the full URL. Other roles see only the scheme, host and port, followed by `/[hidden]`,
  because the path of a webhook URL is the secret part.
- Messages are sent on a background thread, so they never delay the request that caused them. A
  failed delivery is written to the server's log as its kind only (for example `HTTP 500` or
  `TimeoutError`), never with the URL.
- URLs are stored as set in the team database, so protect the database file.

## Data stored and retention

The team database (one SQLite file) holds:

- **Runs.** Each pushed receipt: the receipt JSON (prompts, step summaries, file paths and line counts,
  command text, models, cost and risk reasons) and the HTML receipt.
- **Run history.** When a session is pushed again and replaces the stored run, the replaced version
  (its receipt JSON and HTML, risk score and cost) is kept, at most the newest 20 versions per run.
  The purge deletes a run's history with the run.
- **Approvals.** Their summary, findings, folder, session id, decision, reason and who decided.
- **Keys.** Hashes and prefixes, labels, roles and timestamps. Never the secrets.
- **Settings.** Team webhook URLs, the approval timeout, and the attribution mode.
- **The audit log.**
- **In this release:** budgets and budget alerts, and dashboard sessions as hashes.

**Retention.** By default RunLedger keeps everything until you delete it: with neither retention
variable set, no run, approval, budget alert or audit event is deleted automatically, whatever its
age. Set `RUNLEDGER_RETENTION_DAYS` (for example `90`) and the server deletes runs, their history,
approvals and budget alerts older than that, at start and every hour. The period applies to every
team on the server.

The shortest useful period is 35 days: a value from 1 to 34 is raised to 35 with a warning on
startup. A shorter period could delete the runs of the month a budget alert belongs to, which would
drop the month's spend and fire the same alert again. Budget alerts are kept by the calendar month
they belong to, never by when they fired, so an alert of the current month survives any retention
period.

Audit events have their own period. `RUNLEDGER_AUDIT_RETENTION_DAYS` defaults to 365 days while
runs are being pruned, and `0` keeps them forever; they are never deleted on the runs' schedule, so
`RUNLEDGER_RETENTION_DAYS=35` still keeps a year of audit history. Set the audit variable to keep
audit events for a shorter or longer time independently, or to keep them forever.

To erase all data, stop the server, keep any backup you need, and delete the database file
along with its `-wal` and `-shm` files. That removes every team in the database. See
[self-hosting.md](self-hosting.md) for backups and restores. Retention removes history only;
teams and API keys remain available.

## What RunLedger does not do

- It has per-person accounts, team memberships and OIDC and SAML single sign-on, but no SCIM,
  local passwords or local multi-factor authentication. Enforce MFA at your identity provider.
- Keys do not expire, and there is no IP allow-list.
- Without `RUNLEDGER_RETENTION_DAYS`, runs, approvals, budget alerts and audit events are never
  deleted automatically. There is no per-team retention period.
- One server process uses one SQLite file. It is not a cluster.
- Receipts and the guard's log mask secrets by pattern, as described in [guard.md](guard.md#the-guard-log).
  A secret in an unknown format, or a short one, is not masked. Receipts pushed by clients older
  than 0.3.0 were not masked.

For the checklist to work through before you open a server to your team, see the security checklist in
[self-hosting.md](self-hosting.md#security-checklist).
