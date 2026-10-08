# HTTP API

Every endpoint of the RunLedger team server, with its method, path, authentication, role, request
and response shapes, and errors. The examples are real responses from this release, shortened where
noted with `...`. Replace `https://runledger.example.com` with your server.

Contents:

- [Conventions](#conventions)
- [Health and identity](#health-and-identity)
- [Receipts (runs)](#receipts-runs)
- [Approvals](#approvals)
- [Team settings](#team-settings)
- [API keys](#api-keys)
- [Audit log](#audit-log)
- [Budgets](#budgets)
- [Exports](#exports)
- [Dashboard pages](#dashboard-pages)
- [Error codes](#error-codes)

## Conventions

**Authentication.** Send an API key as `Authorization: Bearer <key>`. The dashboard uses a session
cookie instead: `rl_session`, set by `GET /?key=<key>`.

- A request with an API key needs no extra header.
- A `POST` or `PUT` that uses the session cookie must also send `X-Requested-With: runledger`. Without
  it, the response is `403 csrf_required`.
- A valid API key is checked first. The session cookie is used when the request carries no valid key.

**Roles.** Each key has one role: `viewer` (read only), `member` (also pushes runs and handles
approvals) or `admin` (also manages settings, keys, the audit log, budgets and exports). Each endpoint
below names the minimum role. A higher role always works. See [enterprise.md](enterprise.md#model-teams-keys-and-roles).

**Formats.** Request and response bodies are JSON in UTF-8, except the HTML pages and the exports.
Times are UTC in the form `2026-10-08T16:37:34Z`.

**Errors.** Every JSON error has this shape. Tracebacks are never sent to the client; they go to the
server's log.

```json
{"error": {"code": "forbidden", "message": "The viewer role cannot push runs. This needs the member role or higher."}}
```

**Body size and length.** Every request with a body must send `Content-Length` (otherwise `411`). The
size is checked before authentication, so an oversized request gets `413` even without a key:

| Endpoint | Limit |
| --- | --- |
| `POST /api/runs` | 10 MB |
| `POST /api/approvals` | 128 KB |
| `POST /api/approvals/{id}/decision`, `PUT /api/team/settings`, `PUT /api/budgets`, `POST /api/keys` | 16 KB |

**Rate limit.** An address with more than 20 failed credentials in five minutes gets `429` with a
`Retry-After` header in seconds. `/health` is never limited. Behind a reverse proxy, the address is the
client's only if the proxy's range is configured as trusted (see
[enterprise.md](enterprise.md#https-proxies-and-cookies)). See
[enterprise.md](enterprise.md#rate-limiting).

**Response headers.** Every response has `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`,
`Referrer-Policy: no-referrer` and `X-Frame-Options: DENY`. Over HTTPS (or with the secure-cookie
setting) it also has `Strict-Transport-Security`.

**Methods.** A path that exists but does not allow the method returns `405` with an `Allow` header. An
unknown path returns `404`. On a `GET` endpoint, `HEAD` returns the same status and headers without a
body. Methods the server does not implement, such as `OPTIONS`, return `501`.

## Health and identity

### `GET /health`

No authentication. Use it for load balancers and container health checks.

```json
{"ok": true, "version": "0.2.0"}
```

### `GET /api/me`

Role: any. Returns the caller's team, role and key (never the secret).

```json
{
  "team": {"id": 1, "name": "demo"},
  "role": "admin",
  "key": {"id": "0TQATVTcRECFJjNL", "label": "initial", "prefix": "rl_Xk3vN"}
}
```

Errors: `401`.

## Receipts (runs)

### `POST /api/runs`

Role: `member`. Pushes one receipt. This is what `runledger push` sends: the JSON from
`runledger receipt --format json`, plus `user`, `project`, `html` and per-model costs.

Only `session_id` is required (1 to 200 characters). Other fields are optional and fall back to safe
defaults. Pushing a session again with the same `session_id` replaces the earlier run.

```json
{
  "session_id": "7f3c2a10-9b1e-4c55-a1d2-0e6f8b3c9d42",
  "agent": "Claude Code",
  "agent_id": "claude-code",
  "cwd": "/home/dev/payments-service",
  "git_branch": "fix/retry-logic",
  "started": "2026-10-08T10:00:17.000Z",
  "ended": "2026-10-08T10:06:14.000Z",
  "request": ["The payment retry logic gives up after the first failure..."],
  "overview": "Asked to: ...",
  "risk": {
    "score": 80,
    "level": "high",
    "reasons": [{"severity": "high", "code": "secret_file", "reason": "Read a secrets file (.env)", "step": 3}]
  },
  "totals": {"steps": 10, "tokens": 116416, "cost_usd": 0.108408, "files_changed": 3},
  "models": {"claude-sonnet-4-5-20250929": {"tokens": 79661, "cost_usd": 0.103563}},
  "steps": [{"n": 1, "tool": "Read", "summary": "Read src/retry.py", "model": "claude-sonnet-4-5-20250929", "tokens": 1200, "cost_usd": 0.0012, "error": false, "risks": []}],
  "files": [{"path": "src/retry.py", "added": 12, "removed": 4, "created": false, "deleted": false}],
  "user": "ana@example.com",
  "project": "payments-service",
  "html": "<!doctype html>..."
}
```

Response `201`:

```json
{
  "id": "7f3c2a10-9b1e-4c55-a1d2-0e6f8b3c9d42",
  "url": "/runs/7f3c2a10-9b1e-4c55-a1d2-0e6f8b3c9d42",
  "risk_score": 80,
  "risk_level": "high"
}
```

Stored fields: `user` defaults to `unknown`; `project` defaults to the last folder name of `cwd`;
`risk_level` is derived from the score when absent (below 25 low, below 60 medium, otherwise high).

Errors: `400` `empty_body`, `invalid_json`, `invalid_receipt`; `401`; `403` (viewer); `411`; `413`
(over 10 MB, "Receipts are limited to 10 MB.").

### `GET /api/runs`

Role: any. Lists runs, newest first.

| Query | Meaning |
| --- | --- |
| `user` | Case-insensitive part of the developer name. |
| `project` | Case-insensitive part of the project name. |
| `agent` | Case-insensitive part of the agent name: `claude` matches `Claude Code`. |
| `min_risk` | Only runs with a score of at least this (0 to 100). |
| `limit` | 1 to 500, default 100. |

Each text filter is limited to 200 characters.

```json
{"runs": [
  {
    "id": "7f3c2a10-9b1e-4c55-a1d2-0e6f8b3c9d42",
    "user": "Ana",
    "project": "payments-service",
    "agent": "Claude Code",
    "title": "The payment retry logic gives up after the first failure. Add exponential backoff (3 attempts) and make the tests pass.",
    "started_at": "2026-10-08T10:00:17Z",
    "ended_at": "2026-10-08T10:06:14Z",
    "steps": 10,
    "tokens": 116416,
    "files_changed": 3,
    "cost_usd": 0.108408,
    "risk_score": 80,
    "risk_level": "high",
    "has_html": true,
    "created_at": "2026-10-08T16:37:34Z",
    "updated_at": "2026-10-08T16:37:34Z"
  }
]}
```

Errors: `400` `bad_request` (for example `limit=0`, or `min_risk=101`); `401`.

### `GET /api/runs/{id}`

Role: any. One run with its full receipt JSON and its risk reasons.

```json
{"run": {
  "id": "7f3c2a10-9b1e-4c55-a1d2-0e6f8b3c9d42",
  "user": "Ana", "project": "payments-service", "agent": "Claude Code",
  "title": "...", "started_at": "2026-10-08T10:00:17Z", "ended_at": "2026-10-08T10:06:14Z",
  "steps": 10, "tokens": 116416, "files_changed": 3, "cost_usd": 0.108408,
  "risk_score": 80, "risk_level": "high", "has_html": true,
  "created_at": "2026-10-08T16:37:34Z", "updated_at": "2026-10-08T16:37:34Z",
  "receipt": {"runledger_version": "0.2.0", "session_id": "7f3c2a10-...", "...": "..."},
  "risks": [{"severity": "high", "code": "secret_file", "reason": "Read a secrets file (.env)", "step": 3}]
}}
```

Errors: `401`; `404` `not_found` ("Run not found.").

### `GET /runs/{id}`

Role: any. The stored HTML receipt. It is served with a Content Security Policy that blocks scripts and
external resources. Without a valid credential the response is the JSON `401`.

Errors: `401`; `404` (run not found, or no HTML stored for it).

### `GET /api/stats`

Role: any. Team totals for the last `days` days (1 to 3650, default 30), with breakdowns by developer,
project, agent and model, and the most common risk codes.

```json
{
  "days": 30,
  "since": "2026-09-08T16:37:34Z",
  "totals": {"runs": 1, "cost_usd": 0.108408, "unpriced_runs": 0, "avg_risk": 80.0,
             "high_risk_runs": 1, "steps": 10, "tokens": 116416, "files_changed": 3},
  "by_user": [{"user": "Ana", "runs": 1, "cost_usd": 0.108408}],
  "by_project": [{"project": "payments-service", "runs": 1, "cost_usd": 0.108408}],
  "by_agent": [{"agent": "Claude Code", "runs": 1, "cost_usd": 0.108408}],
  "by_model": [{"model": "Sonnet 4.5", "runs": 1, "tokens": 79661, "cost_usd": 0.103563}],
  "top_risk_codes": [{"code": "secret_file", "occurrences": 2, "runs": 1}],
  "team": "demo"
}
```

Errors: `400` `bad_request` (`days` outside 1 to 3650); `401`.

## Approvals

An approval is a guard request for a person to decide. The guard creates one and polls it. See
[guard.md](guard.md#approvals-on-the-team-server).

Approval fields in responses:

```json
{
  "id": "nNBlegG-prAVQ35f0EsqWQ",
  "status": "pending",
  "decided_by": null,
  "decided_at": null,
  "reason": null,
  "session_id": "s-approval-1",
  "tool": "Bash",
  "summary": "sudo apt-get update",
  "risks": [{"severity": "high", "code": "command", "reason": "Ran a command with sudo"}],
  "cwd": "D:/proj",
  "created_at": "2026-10-08T16:37:34Z",
  "expires_at": "2026-10-08T16:47:34Z"
}
```

`status` is `pending`, `approved`, `denied` or `expired`. A pending approval older than the team's
`approval_ttl_s` reads as `expired`, and so does its `expires_at`. An approval cannot be decided after
it expires.

### `POST /api/approvals`

Role: `member`. Requests a decision.

| Field | Required | Limits |
| --- | --- | --- |
| `session_id` | yes | 1 to 200 characters, no control characters |
| `tool` | yes | 1 to 100 characters |
| `summary` | yes | up to 2000 characters |
| `risks` | no | a list of up to 50 items |
| `risks[].severity` | yes (per item) | `low`, `medium` or `high` |
| `risks[].code` | yes (per item) | up to 64 characters |
| `risks[].reason` | no (per item) | up to 500 characters |
| `cwd` | no | up to 500 characters |

Response `201`:

```json
{"id": "nNBlegG-prAVQ35f0EsqWQ", "status": "pending"}
```

Errors: `400` `invalid_approval` (for example `'risks' must be a list.`); `401`; `403` (viewer);
`411`; `413` (over 128 KB).

### `GET /api/approvals`

Role: any. Lists approvals, newest first.

| Query | Meaning |
| --- | --- |
| `status` | One of `pending`, `approved`, `denied`, `expired`. |
| `limit` | 1 to 200, default 100. |

Response: `{"approvals": [ ...approval fields... ]}`.

Errors: `400` `bad_request` (unknown `status`); `401`.

### `GET /api/approvals/{id}`

Role: any. Returns one approval.

Errors: `401`; `404` `not_found` ("Approval not found.").

### `POST /api/approvals/{id}/decision`

Role: `member`. Approves or denies a pending approval.

```json
{"decision": "approve", "reason": "Checked the package name", "name": "Ana"}
```

| Field | Required | Meaning |
| --- | --- | --- |
| `decision` | yes | `approve` or `deny`. |
| `reason` | no | Up to 500 characters. Stored with the decision and shown on the approval. |
| `name` | no | Up to 100 characters. Recorded in `decided_by`. |

Response `200`: the approval, with `status` set to `approved` or `denied`, and `decided_by` set to
`api`, `api: NAME`, `dashboard`, or `dashboard: NAME` (for decisions made with a session cookie).

Errors: `400` `invalid_decision`; `401`; `403` (viewer); `404` `not_found`; `409` `already_decided`
("This approval was already approved."); `409` `expired`; `411`; `413` (over 16 KB).

### `GET /approvals/{id}`

Role: any. The HTML page where a person can read and decide an approval. Without a valid credential the
response is a sign-in page with status `401`. An unknown id returns the JSON `404`.

## Team settings

### `GET /api/team/settings`

Role: any. Webhook URLs and the approval timeout. For roles other than admin, the URLs show only the
scheme, host and port, followed by `/[hidden]`.

```json
{"team": "demo", "slack_webhook_url": null, "webhook_url": "http://127.0.0.1:8798/[hidden]", "approval_ttl_s": 600}
```

Errors: `401`.

### `PUT /api/team/settings`

Role: `admin`. Changes only the fields you send.

| Field | Type | Rule |
| --- | --- | --- |
| `slack_webhook_url` | string or `null` | `https://` URL, or `http://` for `127.0.0.1` and `localhost` only. `null` or `""` clears it. |
| `webhook_url` | string or `null` | As above. |
| `approval_ttl_s` | integer | 1 to 604800 (seven days). The time a pending approval stays open. |

Response `200`: the same shape as `GET`, with full URLs.

Errors: `400` `invalid_settings` (unknown field, an empty body, a bad URL, a TTL out of range); `401`;
`403` (not admin); `411`; `413` (over 16 KB).

## API keys

Key fields in responses (never the secret, except as noted):

```json
{"id": "wEhJBN8WRNfvQQEC", "label": "dave", "role": "member", "prefix": "rl_Qm7aZ",
 "created_at": "2026-10-08T16:37:38Z", "last_used_at": "2026-10-08T16:37:38Z", "revoked_at": null}
```

### `GET /api/keys`

Role: `admin`. Lists the team's keys, oldest first.

```json
{"keys": [{"id": "0TQATVTcRECFJjNL", "label": "initial", "role": "admin", "prefix": "rl_Xk3vN",
           "created_at": "2026-10-08T16:36:12Z", "last_used_at": "2026-10-08T16:37:33Z", "revoked_at": null}]}
```

Errors: `401`; `403` (not admin).

### `POST /api/keys`

Role: `admin`. Creates a key. The response is the only time the secret is shown.

```json
{"label": "ci-nightly", "role": "member"}
```

`label` is 1 to 100 characters with no control characters. `role` is `admin`, `member` or `viewer`.

Response `201`:

```json
{"id": "DtbXyrhvdmBKxuuu", "label": "carol", "role": "admin", "prefix": "rl_Xk3vN",
 "created_at": "2026-10-08T16:37:38Z",
 "key": "rl_Xk3vN9pQ2rT7wY1zB5cD8fG4hJ6mL0sA2eU9iO3p"}
```

Errors: `400` `invalid_key` (for example `'label' is required.`, or `'role' must be one of admin, member,
viewer.`); `401`; `403`; `411`; `413`.

### `POST /api/keys/{id}/rotate`

Role: `admin`. Replaces the key's secret. The id, label and role stay. The old secret stops working at
once, and dashboard sessions that used the key end. The response contains the new secret once.

Response `200`: the key fields, with `"key"` set to the new secret.

Errors: `401`; `403`; `404` `not_found` ("Key not found."); `409` `key_revoked` (a revoked key cannot be
rotated).

### `POST /api/keys/{id}/revoke`

Role: `admin`. Revokes the key permanently. Its sessions end at once.

Response `200`: the key fields, with `revoked_at` set.

Errors: `401`; `403`; `404` `not_found`; `409` `already_revoked`; `409` `last_admin` (the team's last
active admin key cannot be revoked).

## Audit log

### `GET /api/audit`

Role: `admin`. Events newest first. See [enterprise.md](enterprise.md#audit-log) for what is recorded.

| Query | Meaning |
| --- | --- |
| `limit` | 1 to 500, default 100. |
| `before` | Only events with an id lower than this. Use the id of the last event of the previous page. |
| `action` | *This release.* Only events whose action starts with this text, for example `key.`. |

```json
{"events": [
  {"id": 12, "at": "2026-10-08T16:37:38Z", "actor": "carol (rl_Xk3vN)", "action": "key.revoke",
   "target": "0TQATVTcRECFJjNL", "details": {"label": "initial", "prefix": "rl_Xk3vN", "role": "admin"}}
]}
```

Errors: `400` `bad_request` (`limit` out of range, or `before` not a whole number); `401`; `403`.

`action` takes up to 100 characters. The actions are listed in
[enterprise.md](enterprise.md#audit-log). Budget changes, budget alerts, dashboard sign-ins, and each
export are recorded there too.

## Budgets

*This release.* A budget is a monthly spend limit for the team and for each developer. Budgets only
alert: they do not stop runs or block the guard. See [enterprise.md](enterprise.md#budgets-and-alerts).

### `GET /api/budgets`

Role: any. The team's budget.

```json
{"monthly_usd": 500.0, "per_user_monthly_usd": 60.0, "alert_thresholds": [50, 80, 100]}
```

`null` means no limit.

### `PUT /api/budgets`

Role: `admin`. Replaces the whole budget. A missing amount means no limit, and missing
`alert_thresholds` means `[50, 80, 100]`.

```json
{"monthly_usd": 500, "per_user_monthly_usd": 60, "alert_thresholds": [50, 80, 100]}
```

Rules: each amount is a number from 0 to 1000000000 or `null`. `alert_thresholds` is one to three
whole numbers from 1 to 500, in ascending order. Unknown fields are rejected.

Response `200`: the stored budget, in the same shape as `GET`.

Errors: `400` `invalid_budget` (an unknown field, a bad amount or threshold, or an empty or non-object body;
the message names the problem); `401`; `403`; `411`; `413` (over 16 KB).

Each change is written to the audit log as `budget.update`. Alerts are checked after every push (see
[enterprise.md](enterprise.md#budgets-and-alerts)); a failed alert check is logged on the server and never
fails the push.

### `GET /api/budgets/status`

Role: any. This month's spend (UTC calendar month) against the budget.

```json
{
  "month": "2026-10",
  "spend_usd": 412.37,
  "monthly_usd": 500.0,
  "pct": 82.5,
  "per_user": [{"user": "ana@example.com", "spend_usd": 58.1, "pct": 96.8}]
}
```

`pct` is `null` when there is no limit. `per_user` is sorted by spend, largest first.

Errors: `401`.

## Exports

*This release.* Exports are for compliance records, and they are admin-only. See
[enterprise.md](enterprise.md#exports-and-the-compliance-report) for what the report is and is not.

| Endpoint | Returns |
| --- | --- |
| `GET /api/export/runs.csv?days=N` | CSV: one row per run that started (or was pushed) in the window, oldest first. |
| `GET /api/export/audit.csv?days=N` | CSV: the audit events in the window, oldest first. |
| `GET /api/export/report.html?days=N` | HTML: the compliance report for the window. |

`days` counts whole days back from now (UTC). It defaults to 30 and must be from 1 to 3650.

On a GET request the response is a file download. On a `HEAD` request the response has the same headers
and no body, and nothing is built or audited.

**`runs.csv`** has this header row, and one row per run:

```
id,started_at,user,project,agent,models,steps,tokens,files_changed,cost_usd,risk_score,risk_level,title
7f3c2a10-9b1e-4c55-a1d2-0e6f8b3c9d42,2026-10-08T10:00:17Z,Ana,payments-service,Claude Code,claude-haiku-4-5-20251001; claude-sonnet-4-5-20250929,10,116416,3,0.108408,80,high,The payment retry logic gives up after the first failure. Add exponential backoff (3 attempts) and make the tests pass.
```

`models` lists the model names joined with `; `. The receipt JSON and the HTML receipt are never included.

**`audit.csv`** has the header `id,at,actor,action,target,details_json`. `details_json` is the event's
details as JSON text.

**`report.html`** is one self-contained page with no scripts and no outside resources. Its sections are
totals, runs by risk level, the top risk codes, the high-risk runs, the approvals decided in the period, the
key lifecycle events, the current month's budget status, and a methodology note. Each long table shows at
most 200 rows, with a note when there are more. The CSV exports always hold every row.

Format details:

- CSV files are UTF-8 with a byte-order mark. A text cell that starts with `=`, `+`, `-`, `@`, a tab or a
  carriage return gets a leading apostrophe, so a spreadsheet shows it as text instead of running it.
- File names: `runledger-runs-YYYYMMDD.csv`, `runledger-audit-YYYYMMDD.csv` (attachments) and
  `runledger-report-YYYYMMDD.html` (shown inline), with the date of the request.
- Each GET export is recorded in the audit log as `export.runs`, `export.audit` or `export.report`, with
  `days` and the number of rows or runs in the file.

Errors: `400` `bad_request` (`days` outside 1 to 3650); `401`; `403` (not admin).

## Dashboard pages

### `GET /`

Role: any. The dashboard. Without a valid session it returns a sign-in page with status `401`.

### `GET /?key=<key>`

Signs in with an API key. On success the response is `302` to `/`, with `Set-Cookie: rl_session=...;
Path=/; HttpOnly; SameSite=Lax; Max-Age=43200` (plus `Secure` over HTTPS). On failure it returns the
sign-in page with status `401`, and the failure counts toward the rate limit. A successful sign-in is
recorded in the audit log as `auth.sign_in`, with the role and the client address.

There is no sign-out endpoint. Revoke or rotate the key to end a session.

## Error codes

| Status | Code | When |
| --- | --- | --- |
| 400 | `bad_request` | A query parameter or body value is out of range or not a number. |
| 400 | `empty_body` | The body is empty where JSON is required. |
| 400 | `invalid_json` | The body is not UTF-8 JSON, or is not a JSON object. |
| 400 | `invalid_receipt` | A pushed receipt has no valid `session_id`, or `html` is not a string. |
| 400 | `invalid_approval` | An approval request has a missing or malformed field. |
| 400 | `invalid_decision` | `decision` is not `approve` or `deny`. |
| 400 | `invalid_settings` | Unknown setting, empty settings, a bad URL, or a TTL out of range. |
| 400 | `invalid_key` | A key `label` or `role` is missing or invalid. |
| 400 | `invalid_budget` | A budget value or threshold is invalid (in this release). |
| 401 | `unauthorized` | No valid API key or session. Sent with `WWW-Authenticate: Bearer realm="RunLedger"`. |
| 403 | `forbidden` | The role is too low for the action. The message names the role needed. |
| 403 | `csrf_required` | A cookie-authenticated change lacks `X-Requested-With: runledger`. |
| 404 | `not_found` | No such endpoint, run, approval or key. |
| 405 | `method_not_allowed` | The path does not accept this method. The `Allow` header lists the allowed ones. |
| 409 | `already_decided` | The approval was already approved or denied. |
| 409 | `expired` | The approval expired before a decision. |
| 409 | `already_revoked` | The key is already revoked. |
| 409 | `last_admin` | The team's last active admin key cannot be revoked. |
| 409 | `key_revoked` | A revoked key cannot be rotated. |
| 411 | `length_required` | The request has a body but no `Content-Length`. |
| 413 | `payload_too_large` | The body is over the endpoint's limit. |
| 429 | `rate_limited` | Too many failed credentials from this address. Wait for `Retry-After` seconds. |
| 500 | `internal_error` | An unexpected server error. The details are in the server log. |
| 501 | `server_error` | The method is not implemented, for example `OPTIONS`. |
