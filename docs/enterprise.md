# Enterprise guide: the team server

This guide covers running RunLedger for a team: who can do what, how API keys are issued and retired,
what the audit log records, budgets and alerts, exports, HTTPS, and what the server stores. For the
installation steps, use [self-hosting.md](self-hosting.md). For every endpoint, use [api.md](api.md).

Contents:

- [Model: teams, keys and roles](#model-teams-keys-and-roles)
- [API key lifecycle](#api-key-lifecycle)
- [Audit log](#audit-log)
- [Budgets and alerts](#budgets-and-alerts)
- [Exports and the compliance report](#exports-and-the-compliance-report)
- [Sign-in and sessions](#sign-in-and-sessions)
- [HTTPS, proxies and cookies](#https-proxies-and-cookies)
- [Rate limiting](#rate-limiting)
- [Webhooks](#webhooks)
- [Data stored and retention](#data-stored-and-retention)
- [What RunLedger does not do](#what-runledger-does-not-do)

## Model: teams, keys and roles

- A **team** is the unit of isolation. Teams never see each other's runs, approvals, keys, settings
  or audit events.
- Access is by **API key**. Each key belongs to one team and has one **role**. There are no
  per-person accounts and no single sign-on: a person's access is the key they hold.
- Use one key per person, or per system (for example a CI job), with the least role that works.

| Role | Can do |
| --- | --- |
| **viewer** | Read everything a team member can read: runs, receipts, statistics, approvals, and team settings with webhook URLs masked. Read only. |
| **member** | Everything a viewer can do, plus push runs, request approvals and decide approvals. |
| **admin** | Everything a member can do, plus change team settings, manage keys, read the audit log, and (in this release) set budgets and download exports. |

Roles are ranked: viewer < member < admin. A key's role cannot be changed in place. To change it,
create a key with the new role and revoke the old one.

Access matrix (HTTP endpoints; see [api.md](api.md)):

| Action | viewer | member | admin |
| --- | --- | --- | --- |
| Read runs, receipts, statistics, approvals | yes | yes | yes |
| Read team settings (webhook URLs masked unless admin) | yes | yes | yes |
| Push runs (`POST /api/runs`) | no | yes | yes |
| Request approvals (`POST /api/approvals`) | no | yes | yes |
| Decide approvals (`POST /api/approvals/{id}/decision`) | no | yes | yes |
| Change team settings (`PUT /api/team/settings`) | no | no | yes |
| List, create, rotate and revoke keys (`/api/keys`) | no | no | yes |
| Read the audit log (`GET /api/audit`) | no | no | yes |
| Read budgets (`GET /api/budgets`, `/api/budgets/status`) | yes | yes | yes |
| Set budgets (`PUT /api/budgets`) | no | no | yes |
| Download exports (`/api/export/...`) | no | no | yes |

The last three rows describe this release's budget and export endpoints. The budget reads are open to
every role so that people can see the team's spend; only admins change it.

The command-line key and team commands (`runledger team create`, `runledger key ...`) work on the
database file directly and do not check roles. Whoever can read and write the database file can
create an admin key. Protect the file as you would protect the keys themselves.

## API key lifecycle

A key has an id, a label (1 to 100 characters, no control characters), a role, a prefix, and
creation, last-use and revocation times. The secret looks like `rl_` followed by 40 URL-safe
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

**Rotate.** Replaces a key's secret. The id, label and role stay the same, and the old secret stops
working at once. Dashboard sessions that were started with the key end too. Use this when a key may
have leaked, or on a schedule. A revoked key cannot be rotated.

```bash
runledger key rotate <key id> --db runledger.db
```

```http
POST /api/keys/<key id>/rotate
```

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
| `key.create` | A key is created, including the team's first key | Key id | label, role, prefix |
| `key.rotate` | A key's secret is replaced | Key id | label, role, prefix (of the new secret) |
| `key.revoke` | A key is revoked | Key id | label, role, prefix |
| `run.push` | A receipt is pushed (also when a session is re-pushed) | Run id | agent, project |
| `approval.decide` | An approval is approved or denied | Approval id | status |
| `team.settings` | Team settings change | `settings` | the names of the fields changed, and `approval_ttl_s` if it changed. Never the URLs. |
| `auth.sign_in` | A dashboard sign-in with a key succeeds | Key id | role, and the client address (`ip`) |
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
- **Limits.** `monthly_usd` for the team and `per_user_monthly_usd` for each developer. `null` or `0`
  means no limit.
- **Thresholds.** `alert_thresholds`: one to three whole percentages from 1 to 500, ascending. The
  default is `[50, 80, 100]`.
- **When alerts fire.** After every push. Each threshold fires once per scope per month: once for the
  team, and once for each developer, so a repeated push never repeats an alert.
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
  "per_user": [{"user": "ana@example.com", "spend_usd": 58.1, "pct": 96.8}]
}
```

A budget alert sent to a generic webhook has this shape:

```json
{"type": "budget_alert", "team": "myteam", "month": "2026-10", "scope": "team", "user": null,
 "threshold": 80, "spend_usd": 412.37, "limit_usd": 500.0, "pct": 82.47}
```

## Exports and the compliance report

*This release.* Admins can download three files for a look-back window of `days` days (default 30, from 1
to 3650, counted back from now in UTC):

| Endpoint | Format | Contents |
| --- | --- | --- |
| `GET /api/export/runs.csv?days=N` | CSV | One row per run that started (or was pushed) in the window, oldest first: id, started_at, user, project, agent, models, steps, tokens, files_changed, cost_usd, risk_score, risk_level, title. Never the receipt JSON or HTML. |
| `GET /api/export/audit.csv?days=N` | CSV | Every audit event in the window, oldest first: id, at, actor, action, target, details_json. |
| `GET /api/export/report.html?days=N` | HTML | A report for the window: totals, runs by risk level, top risk codes, high-risk runs, approvals decided, key lifecycle events, this month's budget status, and a note on the method. Each long table shows at most 200 rows. |

The CSV files are UTF-8 with a byte-order mark, and a cell that a spreadsheet could read as a formula
is written as text. The report contains no scripts and no outside resources. Each download is recorded in
the audit log.

Each endpoint also answers `HEAD`, with the same headers and no body. Exports are admin-only.

**What the report is, and is not.** The report and exports help you keep records of what your team's
agents did, what they cost, which risks were flagged, and who decided which approvals. They are
evidence you can file. **They do not make you compliant with SOC 2, the EU AI Act or any other
framework by themselves.** Compliance depends on your controls, policies and processes, on who has
access, and on what you choose to record and keep. Receipts contain prompts and command text, so treat
exports as confidential.

## Sign-in and sessions

- Open `https://your-server/?key=YOUR_KEY` once. The server checks the key, sets a session cookie, and
  redirects, so the key leaves the address bar. Later visits need only the plain address.
- The cookie is `rl_session`: HttpOnly, SameSite=Lax, and valid for 12 hours. It is Secure when the
  server is behind HTTPS (see below).
- A session carries the role of its key. It ends after 12 hours, or at once when its key is revoked or
  rotated.
- *This release:* the server keeps sessions in the team database as hashes of their tokens, so a
  server restart does not sign anyone out. Until this release, sessions were held in memory.
- There is no sign-out endpoint. To end a session, rotate or revoke its key.
- Changes made from the dashboard must send the header `X-Requested-With: runledger`. A cross-site form
  cannot set it, so a forged request is refused with `csrf_required`. Requests that use an API key in
  the `Authorization` header do not need it.

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
approval requests, and for budget alerts in this release.

- URLs must use `https://`. `http://` is accepted only for `127.0.0.1` and `localhost`, for testing.
- Redirects are not followed, so a URL must answer where it was configured.
- Admins see the full URL. Other roles see only the scheme, host and port, followed by `/[hidden]`,
  because the path of a webhook URL is the secret part.
- Messages are sent on a background thread, so they never delay the request that caused them. A
  failed delivery is written to the server's log as its kind only (for example `HTTP 500` or
  `URLError`), never with the URL.
- URLs are stored as set in the team database, so protect the database file.

## Data stored and retention

The team database (one SQLite file) holds:

- **Runs.** Each pushed receipt: the receipt JSON (prompts, step summaries, file paths and line counts,
  command text, models, cost and risk reasons) and the HTML receipt.
- **Approvals.** Their summary, findings, folder, session id, decision, reason and who decided.
- **Keys.** Hashes and prefixes, labels, roles and timestamps. Never the secrets.
- **Settings.** Team webhook URLs and the approval timeout.
- **The audit log.**
- **In this release:** budgets and budget alerts, and dashboard sessions as hashes.

**Retention.** RunLedger keeps everything until you delete it. There is no retention setting and no
delete command for runs or the audit log. To erase data, stop the server, keep any backup you need, and
delete the database file along with its `-wal` and `-shm` files. That removes every team in the
database. See [self-hosting.md](self-hosting.md) for backups and restores.

## What RunLedger does not do

- It has no single sign-on (SAML or OIDC), no per-person accounts and no multi-factor authentication.
- Keys do not expire, and there is no IP allow-list.
- Runs and audit events are never deleted automatically.
- One server process uses one SQLite file. It is not a cluster.
- The receipts are not redacted. A command that contains a token shows that token in the receipt.
- The guard's log masks only known token formats and quoted assignments, as described in
  [guard.md](guard.md).

For the checklist to work through before you open a server to your team, see the security checklist in
[self-hosting.md](self-hosting.md#security-checklist).
