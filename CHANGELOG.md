# Changelog

All notable changes to RunLedger are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions follow
[Semantic Versioning](https://semver.org/); before 1.0, a minor version may still
change behavior, so read the entry before you upgrade.

## [Unreleased]

### Changed

- **Project `.runledger.json` is tighten-only in receipts as well as the guard:** `ignore` and
  lowering `severity_overrides` are read only from your user config (`~/.runledger/config.json` or
  `RUNLEDGER_CONFIG`). A project `.runledger.json` may raise severities and add safe
  `extra_secret_paths`; if it tries to ignore or lower risk kinds, the receipt keeps the findings
  and adds a note.
- **`runledger guard install` writes interpreter-bound hook commands by default:** `guard install`
  writes `"<sys.executable>" -m runledger guard` and `"<sys.executable>" -m runledger checkpoint --hook`
  so the hooks run without `runledger` on `PATH` and when the Python path contains spaces, upgrades
  earlier bare `runledger ...` hook entries in place, and verifies the configured command exits `0`
  on empty JSON input. Pass `--portable` to write bare `runledger ...` commands for checked-in
  settings files.
- **HTML receipts inside a Git work tree are safe by default, and default receipt filenames
  auto-increment (`runledger.cli`):** `runledger receipt` now enables safe mode by default for HTML
  files written inside a Git repository (matching Markdown and JSON; pass `--no-safe` for full
  paths), and when `-o` is omitted it writes `runledger-<stem>-2.<ext>`, `runledger-<stem>-3.<ext>`,
  and so on instead of overwriting an existing receipt file.

### Fixed

- **CLI flag validation, non-colliding receipts, and atomic stem-scoped backups (`runledger.cli`,
  `runledger.server.db`):** `--latest` on `receipt` and `push` is now mutually exclusive with an
  explicit `session` argument (exits `2`); `--open` with `--format md` or `--format json` prints a
  warning to `stderr` instead of being silently ignored; `list --limit` requires a positive integer
  and `checkpoints prune --keep` requires a non-negative integer via `argparse` (exits `2`); and
  `runledger backup` writes stem-qualified `runledger-<db-stem>-YYYYmmdd-HHMMSS[-N].db` files via a
  temporary file and atomic rename (leaving no `.db` or `.tmp` file on failure) while pruning only
  matching database stems.

- **Session discovery, sub-agent merging, and fast `list` (`runledger.parser`, `runledger.session_cache`, `runledger.adapters`):**
  Claude Code sub-agent transcripts (`<project>/<session>/subagents/**/*.jsonl` and legacy
  `<project>/subagents/agent-*.jsonl`) are merged into the parent session's token usage and cost
  with message-id deduplication and a receipt note (`includes N sub-agent transcript(s)`), without
  being listed as separate sessions. Encoded Claude project folder names longer than 200 characters
  resolve via the UTF-16 32-bit base36 hash suffix with a 200-character prefix fallback scan.
  Codex and Droid session discovery and `runledger list` share a corruption-tolerant header and
  summary cache in `~/.runledger/cache/sessions.json` keyed by file modification time and size,
  Droid project discovery filters by encoded folder name before reading unrelated directories,
  Aider discovery walks up from project subfolders to the Git top-level directory, and
  `adapters()` logs broken adapter imports to `stderr` (or re-raises when `RUNLEDGER_DEBUG=1`).
- **Guard project root resolution and subfolder support (`runledger.guard`, `runledger.guard_receipt`, `runledger.risk`):**
  the guard resolves the project root from `CLAUDE_PROJECT_DIR` / `FACTORY_PROJECT_DIR` /
  `DROID_PROJECT_DIR`, else the Git top-level walked up from the event's `cwd`, else `cwd`. Policy
  is read from `<project_root>/.runledger.json`, `.runledger/guard.log` is written only at the
  project root, `write_outside` / `read_outside` / `shell_outside` use the project root as the
  boundary while resolving relative paths against the event's `cwd`, and receipts load both root
  and legacy subfolder guard logs with de-duplication. Running `runledger guard` interactively with
  a TTY stdin now prints help and exits `2` instead of blocking.

- **Unified secret detection and redaction (`runledger.secrets`):** `risk`, `guard` and `llm` share
  one leaf rule set for secret content and secret file paths, resolving `sk-proj-` and 30-character
  GitHub token mismatches, covering `.tfstate`, `.tfvars`, `.jks`, `.keystore`, `.p8`, `.kdbx`,
  `.htpasswd`, `.vault-token`, `.config/gh/hosts.yml`, `.config/gcloud/credentials.db` and
  `.azure/accessTokens.json`, and excluding `id_*.pub` public keys from `secret_file` findings.
- **ReDoS-safe `extra_secret_paths`:** user and project patterns are validated before compilation
  (rejecting nested quantifiers, backreferences and patterns over 200 characters, capped at 20
  patterns and 1,024-character path inputs) and support a `glob:` prefix. Unsafe patterns are
  skipped and logged as warnings by the guard.
- **Git plumbing hardening and faster checkpoint listing:** receipt Git calls strip inherited
  `GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE` and related environment overrides, disable optional
  locks (`GIT_OPTIONAL_LOCKS=0`), treat pathspec magic characters (`[`, `*`, `:`) literally, and
  cache repository context per run. `runledger checkpoints` reads checkpoint metadata in a single
  `for-each-ref` call, and `runledger checkpoint --hook` stays silent outside a Git repository
  while still warning on failures inside a repository.
- **Repository failures are no longer reported as "not a repository" (`runledger.rollback`):**
  `Repository` now distinguishes Git's "not a git repository" message (checked with the C locale)
  from other discovery failures, so a repository Git cannot read (a malformed `.git/config`, a
  permission or ownership error) warns from the checkpoint hook and prints Git's own reason from
  `runledger checkpoint`, instead of being silently swallowed. Checkpoint listing frames each
  `for-each-ref` record with its declared contents size, and any record whose body contains the
  raw record separator is rejected whole, so a malformed foreign commit can no longer truncate
  into a listed checkpoint.
- **PowerShell file write and delete analysis (`runledger.ps_writes`):** `PowerShell` tool steps
  and PowerShell cmdlets inside `Bash` steps (`Set-Content`, `Add-Content`, `Out-File`,
  `Tee-Object`, `Export-Csv`, `New-Item`, `Copy-Item`, `Move-Item`, `Rename-Item`, `Remove-Item`,
  `Invoke-WebRequest -OutFile`, their aliases, `>`/`>>` redirections and `[System.IO.File]` writes)
  now populate `files` in receipts and trigger the same file-path risk rules (`secret_file`,
  `git_internals`, `db_migration`, `shell_outside`, `temp_copy`, `test_deleted`) and guard
  decisions as POSIX shell writes, while skipping variable, wildcard and `-WhatIf` targets.
- **Quote- and comment-aware heredoc scanning (`runledger.shell_syntax`):** `<<` inside single or
  double quotes, `#` comments, `<<<` here-strings and `$((...))` arithmetic expansions no longer
  start a fake heredoc that hides later commands on subsequent lines from `shell_writes` and
  `risk`.
- **Tightened `test_skipped` detection:** `sys.exit(`, `process.exit(`, `os._exit(`, `foo.xit(`,
  `taxit(`, `@pytest.mark.xfail` and `.todo(` in test diffs or edits are no longer misclassified
  as disabled tests, while `xit(`, `xdescribe(`, `xtest(`, `xcontext(`, `xspecify(`, `t.Skip(`,
  `t.SkipNow(`, `self.skipTest(`, `it.only(`, `@Disabled` and `@Ignore` are flagged.
- **UNC, `\\?\` long-path and symlink/junction path handling (`runledger.paths`, `runledger.risk`):**
  UNC paths (`\\srv\share\...` and `//srv/share/...`) and Windows `\\?\` / `\\?\UNC\` long-path
  prefixes are recognized as Windows-style absolute paths across `paths`, `risk` and `guard` while
  keeping device paths (`\\.\...`, `\\?\GLOBALROOT...`) excluded. When a path and working directory
  exist on the local host (non-UNC, matching the host OS style), `_outside` also resolves
  `os.path.realpath` to flag symlink and Windows junction escapes outside the project without
  hanging on unreachable UNC shares.
- **Safe-mode path and Git remote masking (`runledger.safe`):** home folders with spaces in the
  user name (`C:\Users\John Smith\...`, `/home/john smith/...`, `/Users/Mary Jane Doe/...`) are
  masked in full to `~/...`; scp-style `git@host:org/repo` and `ssh://git@host/...` Git remotes are
  preserved instead of being redacted as `[EMAIL]` or `[REDACTED]@`; and foreign non-system
  absolute paths with more than two segments are trimmed to `…/<last two parts>` even when the
  repository root is known.
- **Model normalization, verified OpenAI/Google prices, 1h cache writes and long-context tiers (`runledger.pricing`, `runledger/prices.json`):**
  `normalize_model` strips trailing `[...]` context tags (`claude-opus-4-6[1m]`), `custom:` and
  proxy suffixes (`custom:Claude-Opus-4.5-[Proxy]-0`), Bedrock/Vertex prefixes and `-latest` while
  keeping `-fast`, unverified `-preview` and `-chat-latest` unpriced. `prices.json` adds verified
  OpenAI (`gpt:6-astra`, `gpt:6.1-sol`, `gpt:6-luna`, `gpt:5.6-sol`, `gpt:5.6-cyber`,
  `gpt:5.3-codex`, `gpt:5.4*`, `gpt:5.2*`, `gpt:5.1*`, `gpt:5*`, `gpt:4.1*`, `gpt:4o*`, `o3*`,
  `o4-mini`, `o1*`) and Google Gemini (`gemini:4-argon`, `gemini:3.8-flash`, `gemini:3.6-flash`,
  `gemini:3.5-flash-lite`, `gemini:3.1-flash-lite`, `gemini:3-flash-preview`, `gemini:2.5*`) list
  prices alongside all 20 v0.6.0 Anthropic keys, with backward-compatible optional fields
  (`cache_write_5m`, `cache_write_1h`, `long_context`, `valid_from`, `valid_until`, `windows`),
  once-per-process invalid price file warnings, and a 120-day staleness note in receipts.
  `parser._usage` reads `cache_creation.ephemeral_1h_input_tokens` and bills 1-hour cache writes at
  `2x` input (or `cache_write_1h`), and `cost_of` applies `long_context` rates per request (noting
  when Droid settings-only usage uses the base tier).
- **Codex, Claude Code and Aider usage and prompt parsing (`runledger.adapters.codex`, `runledger.parser`, `runledger.adapters.aider`):**
  Codex `token_count` events deduplicate repeated cumulative totals, fall back to
  `total - _prev_total` when `last_token_usage` is omitted, and handle counter resets; Claude Code
  `parse_session` streams in a single pass and retains user prompts starting with `<` (such as HTML
  or JSX snippets) while still filtering wrapper tags (`<command-`, `<local-command-`,
  `<system-reminder>`, etc.); and Aider parses `cache write` and `cache hit` token counts in either
  order without double-counting `sent` and distributes per-step integer remainders so step tokens
  sum to `totals.tokens`.

## [0.6.0] - 2026-10-15

### Changed

- **Safe mode is the default for shareable receipts:** Markdown and JSON receipts written to
  stdout or inside a Git work tree shorten local paths (repository-relative, `~` for the home
  folder, `…` for shared or `RUNLEDGER_SAFE_PREFIXES` prefixes) and mask e-mail addresses and URL
  credentials. HTML and files outside a repository keep full paths. `--safe` and `--no-safe`
  override the default; JSON receipts add a `safe` key. The GitHub PR comment is always safe.

### Added

- **JSON receipt contract:** JSON receipts carry `schema_version: 1`. `runledger/receipt.schema.json`
  ships with the package and `docs/receipt-json.md` describes every key. Within a version,
  changes are additive only. Markdown receipts on stdout end with
  `<!-- runledger: safe=... schema=1 -->`.
- **Risk for shell and database writes:** files written through the shell get the same path
  checks as edit-tool writes (secrets files high, `.git` folder medium, outside the project low).
  New rules: `db_migration` (medium) for new or changed migration files, `db_write` (medium) for
  migration runners and `sqlite3`/Python commands that change a local database, and `temp_copy`
  (low) for copying a project file into a temp folder. Read-only queries and scratch writes to
  temp folders stay unflagged.
- **Test changes from the Git diff:** each changed test file gets one verdict from its diff
  against HEAD, however it was written: skipped, replaced, weakened or changed. Skipped and
  weakened files add risk and count toward `test_tampering`. Replaced and changed files appear in
  a neutral quality signal, `test_changes`. A diff verdict replaces the per-step Edit rule for
  that file, so a receipt no longer calls one file both weakened and replaced.
- **Receipts after the commit:** when HEAD is newer than the session start (the run's changes
  were committed before `runledger receipt` ran), Git line counts and test verdicts compare the
  working tree with the last commit before the session start instead of HEAD. JSON receipts
  record that commit as `git_base` (null when HEAD was used, including shallow clones and logs
  without a start time).
- **Droid usage source:** JSON receipts include `totals.usage_source` (`messages`, `settings` or
  `missing`) and `tokens_estimated` on each step. Markdown and HTML add a footer note when
  per-step tokens are an even split of the session total.

### Fixed

- **Droid sessions run with `--cwd .`:** the real project folder is recovered from the session's
  `% pwd` context, the settings file, the tool paths' Git work tree or the encoded session folder,
  so these receipts get the branch, repo-relative files and Git line counts. JSON receipts add
  `cwd_source`. `list` and `--latest` find these sessions by their folder name.
- **Search patterns are not paths:** the patterns and scripts of `grep`, `rg`, `ag`, `git grep`,
  `sed`, `awk` and `perl -pe`, and `find -name`/`-path` values, no longer count as paths, so
  `rg '/workspace|/home' src` is no longer flagged `shell_outside`. Only operands in file
  positions are checked (`grep root /etc/passwd` is still flagged), and other quoted regexes
  with `|`, `^`, `.*` or groups are skipped. Quoted grep patterns no longer trigger the
  dangerous-command rules (`grep 'rm -rf /' .`).
- **Relative working folders:** a session whose log records `cwd` as `.` (seen in Droid) no longer
  takes the branch, Git line counts and checkpoints from the folder RunLedger runs in, and no longer
  flags every absolute path as outside the working folder.
- **Safe mode and path lists:** colon-separated path lists (`a:/home/u/b`) are rewritten path by path.
- **Partial costs:** when some tokens have no price, the run cost reads `≥ $X (N models unpriced)`
  and JSON adds `totals.cost_partial` and `totals.unknown_models`. The cost-per-line quality signal
  is skipped for partial costs, and advisor savings leave out steps with no token data.
- **Droid sub-agents and mixed models:** sub-agent tokens from `inclusiveTokenUsage` are counted,
  credited to the child session's model when its settings file is found. Tokens that cannot be
  credited to one model are listed as `unattributed` and no longer trigger the "add a price" help.
- **Large sessions:** shell commands are parsed once per receipt instead of several times, so a
  5,000-step session renders about twice as fast.
- **Droid tokens for copied transcripts:** when a transcript has no sibling `.settings.json`,
  RunLedger finds the session's settings file by id under `$FACTORY_HOME/sessions/`.
- **Timestamps on Python 3.9 and 3.10:** fractions with other than 3 or 6 digits (`00:01:00.5Z`)
  no longer make the run duration or a pushed run's time unknown.
- **Droid receipts without usage:** token counts show `n/a` instead of 0, with a note naming the
  missing settings file. Cost stays unknown.

## [0.5.2] - 2026-10-15

### Added

- **Guard decisions in receipts:** HTML, Markdown and JSON now show blocked calls, local
  approvals and rejections, approval-server outcomes and monitor-only decisions, with summary
  counts. Stream the run's local Guard log, match by tool-use id or legacy tool and summary,
  keep unmatched decisions visible, and mask summaries and reasons. Missing or corrupt logs
  do not prevent receipts.

### Changed

- Guard decision records include `tool_use_id`. The audit log continues to record every
  decision, including plain allows; only receipts filter plain allows from display.

## [0.5.1] - 2026-10-15

### Added

- **User prices:** merge `RUNLEDGER_PRICES` or `~/.runledger/prices.json` over bundled prices.
  Invalid files warn and are ignored. Unknown-price receipts point to the user file and docs.

### Fixed

- **Checkpoints without Git identity:** use a subprocess-only fallback for missing author or
  committer identity, including hooks and rollback safety checkpoints. Preserve configured
  identity fields and never write Git config.
- **Model price lookup:** normalize provider prefixes, case, dotted Claude names, dates,
  deployment versions and `-latest`, while keeping distinct model variants and unknown prices.
- **Windows rollback checks:** detect junctions using file attributes, reparse tags, or the
  Python 3.12+ junction API. Isolate traversal mocks from temporary-index cleanup so rollback
  tests also pass on Windows Python 3.12 and 3.13.

## [0.5.0] - 2026-10-15

### Added

- **Factory Droid adapter:** receipts, risk scoring and session discovery with `--agent droid`,
  including model and token usage from local transcripts and their settings files.
- **Git checkpoints and rollback:** capture tracked and non-ignored untracked files without
  changing HEAD, branches, staging or stashes; list/prune checkpoints, preview restores, select a
  run, and undo with mandatory safety checkpoints. Refuse ignored-file, symlink-parent and
  Windows junction obstructions rather than overwrite or traverse them.
- **Checkpoint hooks and receipts:** Claude Code installation now includes session-start and
  pre-prompt checkpoints. Matching HTML, Markdown and JSON receipts carry a rollback command.
  Droid hook and Codex/manual setup are documented in `docs/rollback.md`.

### Changed

- Removed hosted billing; RunLedger is fully free and open source

## [0.4.1] - 2026-10-10

### Fixed

- **Team run integrity:** receipts are immutable after initial upload. Identical retries remain
  idempotent; conflicting submissions using the same session id now return `409 run_conflict`.
- **Approval integrity:** requests record authenticated requester provenance, and only a separate
  administrator key may decide them. The submitted display name cannot impersonate the approver.
  Legacy approvals lacking requester provenance must be requested again.
- **Webhook hardening:** non-public destinations and DNS rebinding are blocked; local HTTP webhooks
  require an explicit development-only opt-in.
- **Receipt and guard regressions:** corrected stdout `--fail-on` exit codes, native session costs,
  token splits, secret redaction, Windows path handling, Java disabled-test detection and shell
  outside-directory deletion checks.
- **Release safety:** PyPI releases require passing tests and an installed-wheel smoke check.
- **Docker CI reliability:** use the official Python image's public ECR mirror in CI to avoid
  anonymous Docker Hub rate limiting on shared runners; normal Docker builds keep their original base.

### Changed

- The GitHub integration and retention documentation now distinguish sanitized receipts from raw
  transcripts and explain automatic retention when configured.

## [0.4.0] - 2026-10-09

### Added

- **Retention**: `RUNLEDGER_RETENTION_DAYS` deletes older runs, approvals and audit events every hour.
- **`runledger backup`** copies the database while the server runs and keeps the newest copies;
  `deploy/backup.sh` runs it from cron.

### Changed

- `deploy/docker-compose.yml` passes the retention and trusted-proxy settings from `deploy/.env`.
  With them empty, the server behaves as before.

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
