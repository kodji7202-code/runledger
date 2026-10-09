# Changelog

All notable changes to RunLedger are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions follow
[Semantic Versioning](https://semver.org/); before 1.0, a minor version may still
change behavior, so read the entry before you upgrade.

## [Unreleased]

## [0.3.0] - 2026-10-09

### Added

- **Session quality score** (0 to 100, grades A to F) with the signals behind it, and **cost recommendations** with estimated savings, in every receipt.
- **AI risk review** (`runledger receipt --review`, opt-in): Claude Sonnet 5.5 assesses each rule-based risk as confirmed, false positive or uncertain and explains the diff. The rule-based score is never changed.
- **Insights** tab and `GET /api/insights`: quality trend, model and agent comparison, top recommendations, AI false-positive rate. New run filters `min_quality`, `max_quality`, `ai_verdict`; quality and AI columns in exports and the compliance report.
- **`runledger guard uninstall`** (`--project PATH` or `--global`) removes the guard hook from Claude Code's
  `settings.json` and keeps every other setting, including other hooks in the same group. The file is
  backed up to `settings.json.bak` first.

### Changed

- AI step summaries default to Claude Haiku 5.5.
- `runledger list` masks secrets in the first request it prints.

### Security

- **Receipts mask secrets.** The HTML, Markdown and JSON receipt, the payload `push` sends to the team
  server, and the PR comment no longer show secrets from prompts, commands, URLs, search patterns, file
  paths, risk reasons and analysis text. The risk rules still score the raw session, so a hardcoded
  secret is still flagged. A command is masked before it is cut to 120 characters, so the cut cannot
  keep part of a secret. Receipts pushed by older clients stay as they were stored.
- **More secret formats are masked** in receipts, the guard log, approval requests and text sent to
  Claude: Stripe, Google, GitLab, npm and Hugging Face tokens, RunLedger `rl_` keys, JWTs and
  `Authorization: Basic` values; unquoted assignments such as `export DB_PASSWORD=…` and
  `?access_token=…`; flags such as `--token …`; and passwords in URLs. See `docs/guard.md`.

**Known limits in 0.3.0**

- Masking is pattern-based. A secret in an unknown format, or a short one, is not masked.
- The other limits listed for 0.2.0 still apply, except the two about masking.

## [0.2.0] - 2026-10-08

### Added

- **Team server.** `runledger serve` collects receipts pushed from every developer on a team
  (`runledger push`), stores them in one SQLite file, and serves a shared dashboard with per-developer,
  per-model, per-project and per-agent cost and risk totals. Pushing the same session again updates it.
- **Teams.** `runledger team create NAME` creates a team and prints its first admin key once. Teams
  never see each other's runs, approvals, keys, settings or audit events.
- **Roles and API keys.** Every key has a role: `admin`, `member` or `viewer`. Keys can be created,
  listed, rotated and revoked from the command line (`runledger key create|list|rotate|revoke`) and
  through the API (`/api/keys`). A rotated or revoked key stops working at once, and its dashboard
  sessions end with it. A team's last active admin key cannot be revoked.
- **Audit log.** Key changes, pushed runs, approval decisions, team settings changes and dashboard
  sign-ins are recorded with the actor, time and target. Readable by admins at `GET /api/audit`.
- **Approvals.** A guard call marked "ask" can wait for a person. Decisions are made on the dashboard
  or through the API, and each request can expire after a team-wide time limit (`approval_ttl_s`). New
  approvals can notify a Slack incoming webhook and a generic webhook.
- **Team settings.** `GET` and `PUT /api/team/settings` for the webhook URLs and the approval time limit.
- **Dashboard.** Statistics, runs, pending approvals, and for admins the keys, audit log and team
  settings. Dashboard sign-in uses a key once and then a session cookie.
- **Real-time guard.** `runledger guard` is a Claude Code `PreToolUse` hook. It allows, asks for
  approval, or denies each tool call against a policy. `runledger guard install` adds it to a project
  (`--project PATH`) or to every project (`--global`), and `runledger guard test` shows the decision for
  one event. Policy comes from the user's `~/.runledger/config.json` and the project's
  `.runledger.json`. Monitor mode logs decisions without blocking. Fail-closed is optional. Each
  decision is logged to `.runledger/guard.log`. See `docs/guard.md`.
- **Agent adapters.** Codex CLI (`--agent codex`), Aider (`--agent aider`), and the RunLedger open
  format for any other agent (`--agent native`, `docs/format.md`). `list`, `receipt` and `push` accept
  `--agent`. `list --all` covers Claude Code and Codex sessions.
- **GitHub pull requests.** A composite action (`action.yml`) and `python -m runledger.github` post one
  sticky receipt comment per pull request, with the highest risk and a row per session. See
  `docs/github.md`.
- **Self-hosting.** A Docker image, a Docker Compose stack with Caddy for automatic HTTPS, and a
  bare-metal guide. The server takes its own TLS certificate (`--tls-cert`, `--tls-key`, TLS 1.2 and
  later), a secure-cookie setting (`--secure-cookies` or `RUNLEDGER_SECURE_COOKIES=1`), and a
  reverse-proxy setting (`--trust-proxy`). See `docs/self-hosting.md`.
- **Budgets and alerts** (this release). A monthly spend limit for the team and optionally for each
  developer, with alert thresholds. Alerts fire once per threshold per scope per month, after a push,
  to Slack and the webhook. `GET` and `PUT /api/budgets`, and `GET /api/budgets/status`. Budgets only
  alert; they do not block runs.
- **Exports** (this release, admins only). `GET /api/export/runs.csv`, `GET /api/export/audit.csv` and
  `GET /api/export/report.html`, for the last `days` days (default 30). `HEAD` returns the headers only.
  Each download is recorded in the audit log. The report summarises runs, risk levels, approvals and
  budget status. It does not by itself make you compliant with any framework.
- **Audit filter** (this release). `GET /api/audit?action=` returns events whose action starts with the
  given text, for example `key.`. Sign-ins are recorded with the client address.
- **Trusted proxies** (this release). `serve --trusted-proxy CIDR`, repeatable, or the comma-separated
  `RUNLEDGER_TRUSTED_PROXIES`. From a trusted proxy, the client address is the right-most untrusted
  `X-Forwarded-For` address. It is used for the failed-sign-in limit and the audit log. `--trust-proxy`
  honours `X-Forwarded-Proto` only from those proxies when they are configured.
- **Persistent dashboard sessions** (this release). Sessions are stored in the team database as hashes, so
  a server restart does not sign everyone out.
- **Documentation.** A new README, and `docs/quickstart.md`, `docs/agents.md`, `docs/guard.md`,
  `docs/enterprise.md` and `docs/api.md`.
- **Packaging.** Apache License 2.0, PyPI metadata, continuous integration on Linux, Windows and macOS
  for Python 3.9, 3.12 and 3.13, and a release workflow that publishes version tags to PyPI.

### Changed

- **Risk scores.** Medium findings add at most 45 points in total, and low findings at most 10, so a long
  session full of small findings cannot reach 100 on its own. High findings are not capped. The total is
  capped at 100, and a repeat of the same code adds a third of its points, as before. Scores for the same
  session can differ from 0.1.0.
- **Risk rules.** The shell parser was rewritten. It splits commands at `;`, `&&`, `||`, `|` and newlines,
  ignores heredoc bodies and comments, and does not read the text arguments of `echo`, `printf`,
  `git commit -m`, `python -c` and similar commands as paths or secrets. PowerShell has its own rules.
  Drive paths in Git Bash, MSYS, Cygwin and WSL form are recognised when the working folder is a Windows
  path. Temp folders are not treated as outside the project.
- **Receipt JSON.** Adds `agent` (the agent's name, for example `Claude Code`) and `agent_id` (for example
  `claude-code`).
- **Sessions without an agent.** `runledger receipt` with no session file uses the newest session of any
  agent in the folder. Earlier versions read Claude Code sessions only.
- **Costs.** The list-price table moved from the code into `runledger/prices.json` (checked 2026-10-08), and
  `RUNLEDGER_PRICES` can override entries. Where an agent reports its own cost (Aider, and the RunLedger
  format), the receipt shows it next to the estimate.

### Security

- **API keys** are stored only as SHA-256 hashes. The secret is shown once, when it is created or rotated.
  Only the first eight characters are kept for display.
- **Roles** limit every endpoint. Viewers read; members push runs and handle approvals; admins manage
  settings, keys and the audit log.
- **Dashboard cookies** are HttpOnly with SameSite=Lax. Over HTTPS, or with the secure-cookie setting,
  they also carry the Secure flag and the server sends `Strict-Transport-Security`. Changes made with a
  cookie must send `X-Requested-With: runledger`, which blocks cross-site forms.
- **Failed credentials** are counted per address. More than 20 in five minutes returns `429` until they
  age out.
- **Webhook URLs** must use `https://`, except `http://` to `127.0.0.1` and `localhost`. Redirects are not
  followed. Non-admin users see only the scheme, host and port. The URLs are never written to the audit log
  or the server log.
- **Request limits.** Receipts up to 10 MB; approval requests up to 128 KB; decisions, settings and key
  requests up to 16 KB. Bodies need a `Content-Length`. Limits are checked before authentication.
- **Responses** carry `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`, `Referrer-Policy:
  no-referrer` and `X-Frame-Options: DENY`. The dashboard uses a Content Security Policy with a
  per-page nonce, and HTML receipts are served with a stricter one. Error responses never include a
  traceback.
- **Guard policy.** A project's `.runledger.json` can only tighten the guard. The approval server and its
  key come only from the environment or the user's own file, so a cloned repository cannot send the key
  elsewhere. Fail-open is the default, and `fail_closed` turns it off.
- **Guard log and approval requests** mask known token formats and quoted secret values.

**Known limits in 0.2.0**

- Receipts are not redacted. A token typed into a command appears in the receipt.
- Guard masking covers known token formats and quoted `key="value"` assignments. It does not mask unquoted
  assignments, short values or tokens in URLs.
- The failed-credential counter uses the connection's address. Behind a reverse proxy, that is the proxy's
  address, until trusted proxies are configured.
- There is no single sign-on, no per-person account, no multi-factor authentication, and no key expiry.
- There is no sign-out endpoint. Sessions end when their key is revoked or rotated, or after 12 hours.
- Runs and audit events are never deleted automatically. There is no retention setting.
- One server process uses one SQLite file. It is not a cluster.

## [0.1.0]

First working prototype (MVP).

### Added

- Receipts for a Claude Code session as HTML, Markdown or JSON: files changed with lines
  added and removed, every step in plain language, the model, tokens and estimated cost per
  step, and a risk score from 0 to 100 with reasons.
- `runledger list`, `runledger receipt` (with `--open`, `--format`, `--fail-on`) and
  `--ai` for plain-language summaries from Claude Haiku.
- Public Claude API list prices in `runledger/prices.json`, with `RUNLEDGER_PRICES` to
  override them.
- No runtime dependencies; Python 3.9 and later.
