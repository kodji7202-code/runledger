# Changelog

All notable changes to RunLedger are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions follow
[Semantic Versioning](https://semver.org/); before 1.0, a minor version may still
change behavior, so read the entry before you upgrade.

## [Unreleased]

### Added

- **Selective restore with `runledger rollback <id> --path P` (`runledger.rollback`):** the option
  (repeatable) restores only the named file or folder and leaves everything else exactly as it is.
  A value is read against `--cwd` or may be an absolute path inside the repository; matching is
  literal, so `src` restores `src/x.txt` but never `src2/y.txt`, and names with `[` or `*` behave.
  Directories restore their whole subtree, including the deletion of files created after the
  checkpoint (labelled `delete`, not in checkpoint); a value that is already identical prints
  `Nothing to restore for <path> (already matches <id>).` and exits 0. Paths outside the
  repository, `.git` and its contents, empty values and values present in neither the checkpoint
  nor the working tree are refused with a one-line error before anything is written. All the
  whole-tree safety stays in place: one safety checkpoint of the whole tree is still created
  first (so `runledger rollback <safety-id>` undoes a partial restore), the stale-preview check
  still covers the whole tree, and the symlink, junction, `.git` overlap, reserved-name and
  case-collision refusals apply to an in-scope path exactly as before. `--path` combines with a
  checkpoint id, `--run`, `--last`, `--dry-run` and `--yes`, and the preview lists the scope, each
  change, and a summary with the number of files and the bytes to restore. `--path .` is the same
  as a whole-tree rollback, which is unchanged without the option.
- **Droid discovery follows `FACTORY_HOME_OVERRIDE` (`runledger.adapters.droid`):** the variable
  is read as a HOME, so the Factory config directory becomes `<FACTORY_HOME_OVERRIDE>/.factory`,
  and that is where sessions, settings files and a global hook file are looked for.
  `FACTORY_HOME`, which names the config directory itself, still wins over it; without either,
  the directory is `~/.factory`.
- **MCP calls are graded by their verb (`runledger.mcp_risk`, new; used by `runledger.risk`):** a
  destructive tool (`delete_project`, `drop_table`, `reset_branch`, `purge_bucket`) is a **high**
  `mcp` finding with the reason `Called a destructive MCP tool (<server>: <tool>)`, a
  state-changing tool (`create_post`, `send_message`, `deploy`) is medium, and a read-only or
  unknown name keeps the previous low finding and its wording. Words are matched exactly and case
  insensitively, so `deleted_items` and `undelete_items` stay low while camelCase and kebab names
  (`deleteBranch`, `delete-branch`) split into words; a leaf whose first word is a read verb
  (`get_delete_policy`) never escalates. Both name shapes agents report are understood:
  `mcp__<server>__<tool>` (Claude Code, Codex) and the bare connector name
  (`supabase__delete_project`, Factory Droid).
- **The SQL argument of an execute-class MCP tool is inspected (`runledger.mcp_risk`, new):** a
  call whose name carries `sql`, `query`, `statement` or `execute`, or that carries a
  `query`/`sql`/`statement` argument that opens like a statement, is graded by that statement
  instead of its verb: `DROP`, `TRUNCATE`, a `DELETE` or `UPDATE` without a `WHERE` and
  `ALTER TABLE ... DROP` are high, any other data-changing statement is medium, read-only SQL
  keeps the low finding, and a call with no SQL the guard can read is medium. Strings and comments
  are blanked before matching, so `select 'DROP TABLE x'` stays low. The SQL body is never copied
  into a reason, `guard.log`, an approval request or a receipt, and an argument of up to 2 MB is
  capped at 20 000 characters before any pattern runs.
- **Twenty-two more dangerous-command rules (`runledger.risk`):** force-deleting a git branch
  (`git branch -D`, `git branch --delete --force`), discarding all working-tree changes
  (`git checkout .`, `git restore .`), `git stash drop`/`clear`, `git push --delete` and
  `git push origin :branch` (high), `find ... -delete` and `find ... -exec rm|unlink|shred`,
  `docker system|builder|image|container|network prune`, `gh <resource> delete`, `aws s3 rm` of a
  single object, `rsync --delete`, `dd of=<file>` and `reg delete` of a user key (medium),
  `helm uninstall`, `gh repo delete`, `docker volume prune` and `docker system prune --volumes`,
  `aws s3 rm --recursive`, `aws s3 rb --force`, `dd of=/dev/...`, `rd /s /q`, a shell
  `DELETE FROM` without a `WHERE` and `reg delete` of a machine-wide key (high), plus PowerShell
  `Remove-Item -Recurse` without `-Force` (medium; the existing `-Force` rule stays high and does
  not also report it). Every rule is confined to one command (the lookahead stops at `;`, `&`, `|`
  and newlines) and runs on the quote-, comment- and heredoc-blanked view, so a commit message or a
  string that mentions `git branch -D` is not flagged.
- **Rules you write yourself (`command_rules`, `tool_rules`, `mcp_verbs`):** the user file
  (`~/.runledger/config.json`) and a project's `.runledger.json` may now carry three top-level
  keys. `command_rules.deny`/`.ask` take regular expressions, or objects with `pattern`, an
  optional `reason` and `raw`; they are searched case-insensitively against the same
  quote- and heredoc-blanked view the built-in command rules use, and `raw: true` reads the whole
  command instead. `tool_rules.deny`/`.ask` take `fnmatch` globs matched against the whole tool
  name, case-sensitively, for any tool (`Edit`, `mcp__vercel__*`); a tool on both lists is denied.
  `mcp_verbs.destructive`/`.write`/`.read` add words to the MCP verb vocabularies, so a server
  whose destructive tool is named something else is graded as expected. Hits are `rule_deny`
  (high) and `rule_ask` (medium) findings whose reason names the entry. A rule hit is always
  decided: replacing the default deny list keeps it, and no `!` exclusion can drop it. Both files
  only add, so a project can tighten with rules but never remove one of yours; a project
  `mcp_verbs.read` list, a project exclusion aimed at a rule hit and a pattern that is not a safe
  regular expression are ignored and logged as warnings. Receipts: a receipt re-runs the rules with
  the project's file only, so a rule that lives in your user file is enforced live but does not
  appear in a receipt built from an older session (`docs/guard.md`).
- **The guard reads Claude Code, Factory Droid and Codex payloads (`runledger.guard`):** the hook
  command carries `--agent claude-code|droid|codex` (without the flag the agent is inferred from the
  payload, and Claude Code is the default, so existing hooks are unchanged), so a Droid `Execute`
  is decided as the shell call it is, `Create`/`Edit` as writes and edits, `ApplyPatch`/`apply_patch`
  as one step per file the patch touches (one bad file denies the whole call, and the log lists the
  paths, never the bodies), its searches as `Search` and `FetchUrl` as `WebFetch`; MCP names keep
  both shapes (`mcp__server__tool` and Droid's bare `server__tool`). The log keeps the agent's own
  tool name and adds `agent` and `canonical_tool` (`Execute` becomes `Bash`), and `session_id` stays
  the agent's own session id, so a receipt can match the decisions to the session. Codex cannot
  prompt from a hook, so an ask becomes a **deny** there with a reason that names the rule and how
  to proceed (`guard.codex_ask: "allow"` in the user file opts out; a project file cannot set it),
  while Claude Code and Droid keep asking. PowerShell commands sent under `Bash` (Codex) or
  `Execute` (Droid) are analysed as PowerShell. An unknown `--agent`, a malformed payload or an
  internal error never blocks a call: the hook exits 0, keeps the fail-open/fail-closed semantics
  and reports a bad agent on stderr. A hook command stored with `--agent` is recognised as
  RunLedger's own by `install`, `uninstall` and the install-time self-test, `guard test --agent X`
  reads a native payload, and the hook stays fast and offline (`docs/guard.md`).
- **One `guard install` for Claude Code, Droid and Codex (`runledger.hook_targets`, new;
  `runledger.guard`):** `runledger guard install|uninstall [--agent claude-code|droid|codex|all]
  [--project PATH | --global]` writes each agent's own file. Claude Code keeps
  `<project>/.claude/settings.json` / `~/.claude/settings.json` (the unchanged default); Factory
  Droid gets `<project>/.factory/hooks.json` / `<FACTORY_HOME>/hooks.json` (or
  `<FACTORY_HOME_OVERRIDE>/.factory`), keyed by event with **no** `hooks` wrapper and never its
  `settings.json`; Codex gets `<project>/.codex/hooks.json` / `<CODEX_HOME>/hooks.json`, with the
  events wrapped in `hooks`, matcher `.*` and `config.toml` never touched. The guard command carries
  the agent (`"<python>" -m runledger guard --agent droid|codex`; `--portable` keeps the bare form),
  the checkpoint hooks stay `runledger checkpoint --hook` for all three, and `install` runs the
  stored command once. Installs are idempotent (one guard entry per file, `<file>.bak` written once
  and never rewritten), keep every foreign hook and key, refuse a malformed or wrongly shaped file
  untouched (exit 1, nothing written, no `.bak`/`.tmp`) and accept an empty or BOM-prefixed one;
  `uninstall` removes only RunLedger's entries plus the groups, events and `hooks` object they
  emptied, and an agent without a hook file is reported without creating one. `--agent all` installs
  in a project for all three and, globally, only for agents whose config folder already exists (it
  never creates `~/.factory` or `~/.codex`, and names each skipped agent with the reason). `guard
  test --agent droid|codex` prints the decision and that agent's hook output, and the install output
  says what each agent still needs: Droid snapshots its hooks at startup (restart or `/hooks`), and
  Codex runs only hooks you trust and turns an ask into a deny (`guard.codex_ask`) - neither message
  claims the hook is verified inside the agent, which only a real session shows
  (`docs/guard.md`, `docs/agents.md`, `docs/rollback.md`).
- **`runledger doctor` and `runledger guard status` (`runledger.doctor`, new):** nine checks in one
  report, with `--json`, `--agent`, `--project`, `--offline`, `--no-selftest` and `--verbose`, and
  exit codes 0 (ok/info/skip), 1 (a warning) and 2 (a failure). It reports the install (version,
  Python, how the CLI is reached), the guard hook of every agent and scope (`info` when nothing is
  installed, `ok` with the entries found, `warn` for a missing checkpoint entry, a duplicate guard
  entry, a hook timeout shorter than the approval wait or a `--portable` command whose `runledger`
  is not on `PATH`, `fail` for unreadable JSON - with the line - or a missing interpreter), the
  Codex trust and Droid restart notes, a real self-test that runs the installed command string
  through the shell in a temporary folder with a temporary HOME (deny JSON for `rm -rf /`, silence
  for `ls`, elapsed time in the detail; `--no-selftest` starts no subprocess), both config files
  (invalid JSON and project keys that try to loosen the guard are named), the approval server
  (unauthenticated `GET /health`, 3 s, `--offline` makes no request; the key is reported only as
  set or not set), the guard log (size, last decision, error rows of the last 7 days, `.gitignore`
  coverage; never its contents), the checkpoints (Git version, count, newest tree size, a warning
  above 100), the bundled prices (120-day staleness, `fail` when the table is unreadable) and the
  sessions per agent with the home directory each one was read from. `guard status` is the compact
  guard-only view - hooks, notes, config, approval, log and the effective mode with `fail_closed` -
  and runs no self-test. Both commands are read-only: no hook file, config, log, checkpoint ref or
  cache is written, and no secret is printed (`docs/doctor.md`).
- **`runledger stats` (`runledger.stats`, new):** one report over the local sessions - sessions,
  steps, tokens and cost per group, the risk and quality distributions, the most common risk codes
  and advisor recommendations, and the guard decisions in the window. `--project PATH` or `--all`,
  `--agent`, `--since 7d|4w|1m|YYYY-MM-DD` (default `30d`), `--until`, `--by agent|model|day|week|
  project` (default `agent`), `--top 5`, `--format table|json|md`, `--limit 500` (newest sessions in
  the window, with a stderr note when it truncates), `--no-cache` and `--rebuild-cache`. Cost stays
  honest: a model with no price gives `n/a`, a session that mixes priced and unpriced tokens gives a
  `>=` lower bound with a footnote naming the unknown models, and no figure is ever a fake `$0.00`.
  Exit codes are 0 (a report), 1 (no sessions for the scope, or none in the window, with the reason
  on stderr) and 2 (usage error). The window is resolved from the current minute, so two runs in the
  same minute agree. The command is read-only and runs no Git, no network request and
  no AI: it parses each session once and caches the tokens per model together with the
  per-request token numbers pricing needs, the risk result, the grade and the recommendation
  kinds in `~/.runledger/cache/stats-v1.json`, keyed by path + mtime + size and invalidated by a
  change to the user config, to the project's `.runledger.json` or to any file the parse read
  beside the transcript (a Droid `<id>.settings.json` sidecar, sub-agent transcripts). Aider
  history files are looked for in the project folder only, because the search that walks up to
  the Git top level would run Git. The cache holds no
  prompts, commands or file contents, and a corrupt session file is listed in `skipped` instead of
  stopping the run (`docs/stats.md`).
- **`runledger checkpoints prune` takes `--older-than DUR` and needs only one of the two options
  (`runledger.rollback`, `runledger.cli`):** `--keep N` still protects the newest N checkpoints and
  `--older-than 30d` (`2w`, `12h`) removes the checkpoints older than that; neither is required on
  its own, but giving neither is an error (`--keep or --older-than is required for prune.`, exit 1),
  and a duration that is not a number followed by `d`, `w` or `h` is refused by the parser before
  anything is deleted (exit 2). Giving both is conservative - a checkpoint goes only when it is
  **both** beyond the newest N **and** older than the limit, and one exactly at the limit age is
  kept - so the rules can never add up to a wider deletion than each one alone. The age comes from
  the ref's own Git commit date (the date `GIT_COMMITTER_DATE` writes), so a repository with old
  checkpoints prunes them by how old Git says they are. Safety checkpoints are pruned like any
  other, the report names the count and how many of them were safety checkpoints, and only refs are
  deleted: the commits stay readable until Git reclaims them, and `git gc --prune=now` is mentioned
  as text only, never run (`docs/rollback.md`).
- **The checkpoint hooks prune automatically (`runledger.rollback`):** after the checkpoint of a
  `SessionStart` or `UserPromptSubmit` hook exists, retention runs under the user's own
  `~/.runledger/config.json` settings - on by default, keeping the newest 50 checkpoints with no age
  limit, tunable with `{"rollback": {"auto_prune": {"keep": N, "older_than": "30d"}}}` and
  switchable off with `"auto_prune": false`. The checkpoint just created and every checkpoint of the
  session in progress are never removed, so `rollback` and `rollback --run` always reach the run the
  agent is in; an unusable config keeps the documented default instead of guessing, and a failure
  inside auto-prune is swallowed - a hook never blocks the agent or loses the checkpoint it just
  made. Only refs are deleted, and nothing is written to stdout, where Droid and Codex turn hook
  output into agent context.
- **A size warning for large checkpoints (`runledger.rollback`, `runledger.cli`):** `runledger
  checkpoint` and the checkpoint hooks measure the snapshot (`git ls-tree -r -l`, one call) and
  print one `warning:` line to stderr when it exceeds `rollback.warn_mb` (default 500 MB of content)
  or `rollback.warn_file_mb` (default 50 MB for a single file, `0` switches a threshold off), naming
  the total, the file count, the largest file and its size, and pointing at `.gitignore`, which is
  the real fix because a checkpoint takes the whole non-ignored tree. The manual command warns every
  time and never puts the text on stdout; the hooks warn on stderr only, at most once per session
  per 24 hours (marker file `<repo>/.runledger/.size-warned`), so a huge tree does not repeat the
  message on every prompt. A tree above 200,000 entries skips the scan instead of costing every
  hook, and both thresholds at `0` skip it entirely.
- **A project `.runledger.json` cannot change checkpoint retention (`runledger.guard`):**
  `rollback.auto_prune`, `rollback.warn_mb` and `rollback.warn_file_mb` are read from the user's own
  config file only, and a project file that sets them is ignored with a warning from the guard and
  from `runledger guard test` (`Ignored rollback.auto_prune in .runledger.json: a project file
  cannot change checkpoint retention or the size warning, ...`), so a cloned repository can neither
  delete the user's checkpoints nor silence the size warning.

### Changed

- **A `--global` Claude Code install follows `CLAUDE_CONFIG_DIR` (`runledger.guard`):** the user
  settings file is `$CLAUDE_CONFIG_DIR/settings.json` when that variable is set - the file Claude
  Code itself reads - instead of always `~/.claude/settings.json`.
- **Destructive MCP calls ask by default (`runledger.guard`):** a high `mcp` finding now matches the
  default `ask: ["severity:high"]`, so `mcp__supabase__delete_project`, and an execute-class call
  whose SQL drops or rewrites rows, wait for confirmation instead of running. Nothing is denied by
  default (`mcp:high` must be added to a `deny` list), and a user file can soften it again with
  `severity_overrides` or `ignore`. The new command rules can turn a previously allowed call into a
  deny (`git push origin --delete x`, `docker volume prune`, `helm uninstall`, `gh repo delete`) or
  add a finding to it (`git branch -D x`, `rsync --delete`, `Remove-Item -Recurse`).
- **`nuke` is deliberately not one of the built-in destructive MCP words (`runledger.mcp_risk`):**  a tool whose name carries it stays low until you add it to `mcp_verbs.destructive`. The rest of
  the vocabulary (`delete`, `drop`, `destroy`, `reset`, `purge`, `terminate`, `force`, `revoke`
  and the other words) is unchanged, and users who never set `mcp_verbs` see the difference only
  for names built on `nuke`.
- **The user's config file is read in one place (`runledger.userconfig`, new):** `RUNLEDGER_CONFIG`
  or `~/.runledger/config.json` is resolved and parsed by one module, which the guard, the risk
  policy and the new `rollback.load_settings()` consume, so a missing or broken file is handled the
  same way everywhere. `rollback.load_settings()` returns the documented `rollback` settings with
  their defaults (auto-prune on, keeping the newest 50 checkpoints, no age limit; size warnings at
  500 MB in total or 50 MB for one file, 0 switches a warning off), and `pricing.prices_checked()`
  exposes the bundled `prices.json` verification date as a `date` (or `None` when the packaged
  table carries no usable `checked` value) for the price-staleness checks.

### Fixed

- **`rollback --path` reads a relative value against the invocation folder
  (`runledger.rollback`):** `--cwd REPO/sub --path a.txt` resolved `a.txt` against Git's top
  level, so it restored `REPO/a.txt` and left the intended `REPO/sub/a.txt` modified. A relative
  value is now read against the folder the command was invoked with (`--cwd`, or the current
  folder without it), and a value that only exists relative to the top level is refused with
  `no such path` instead of being restored from there.
- **A folder selection folds case on a case-insensitive volume (`runledger.rollback`):** only the
  file entries carried a case-folded alias, so `--path SUB` on Windows selected nothing and left
  `sub/c.txt` modified, and a deleted folder named with a different case was reported absent.
  Folder names are part of the same spelling table now, so a value selects the repository's own
  spelling for a file or a folder, existing or deleted.
- **Case folding happens only where the volume folds case (`runledger.rollback`):** the retry ran
  on every platform, so on a case-sensitive Linux volume an absent `--path README.md` selected and
  restored an existing `readme.md` the value never named. Whether a volume folds case is decided
  once per repository (Windows path rules, or asking the folder for a different case), so a
  case-sensitive volume keeps every value literal and refuses the absent path (`docs/rollback.md`).
- **A blocker outside a `--path` selection no longer stops the restore (`runledger.rollback`):**
  the whole-tree snapshot refused any unignored junction in the repository, so one unrelated
  junction made `rollback <id> --path a.txt` fail with "Windows junctions and other non-symlink
  reparse points are not supported". The reparse-point scan now covers the selection and the way
  to it, while an in-scope junction is still refused by name before anything is written; the
  whole-tree refusal, the whole-tree stale-preview check and the safety checkpoint are unchanged.
- **The full-tree rollback preview reports the bytes it would change (`runledger.cli`):** a
  `--dry-run` without `--path` printed its restore and remove counts without the byte total the
  scoped preview already had. The summary line carries it now (`1 restore, 0 remove (42 bytes).`);
  restoring is unchanged.
- **`runledger stats` prices a session exactly as `receipt` does (`runledger.stats`):** the
  cached token buckets keep the per-request boundaries the pricing path reads, so a session of
  several short requests is no longer charged a long-context tier for its total: two 60,000-token
  Haiku requests cost $0.012 in `stats`, cold and warm, exactly as in `runledger receipt`, instead
  of the $0.06 a single aggregate request would cost. Only token and request-tier numbers are
  cached - never a prompt or a command - and a price-table change still needs no invalidation.
- **`runledger stats` never runs Git on any path (`runledger.stats`, `runledger.adapters.droid`):**
  two paths still started `git rev-parse`: a Droid transcript that records a relative `cwd` and can
  only be placed by its tool paths, and `--agent aider`, which used the full Aider search that walks
  up to the Git top level. The Aider search now stays in the project folder for every invocation
  (the walk up is Git), and a relative Droid `cwd` is recovered from the nearest ancestor holding
  `.git` without starting a process, so `stats` starts no child process at all.
- **`--by model` splits a mixed-model session (`runledger.stats`):** a session that used several
  models contributes its tokens and cost to each of their rows, priced per model, instead of the
  whole session being credited to the model that used the most tokens; a 2000-token Sonnet and
  1000-token Haiku session now reports two groups whose cost matches `receipt`. Its session and
  step counts stay with the dominant model, so every column still sums to the `total` row
  (`docs/stats.md`).
- **A stats cache record watches every file the parse read (`runledger.stats`, `runledger.parser`,
  `runledger.adapters.droid`):** the record was invalidated by the transcript alone, so editing only
  a Droid `.settings.json` sidecar or a Claude sub-agent transcript left warm stats reporting the
  old tokens and cost while `--no-cache` saw the new ones. Both files are now recorded with their
  size and modification time and re-parse the session when they change.
- **A prices file with a UTF-8 BOM is read (`runledger.pricing`):** `~/.runledger/prices.json`
  and a `RUNLEDGER_PRICES` file written by a Windows editor or by `Set-Content -Encoding UTF8`
  start with a byte-order mark, which the JSON reader rejected, so the whole override was
  ignored with a warning. The file is now decoded as `utf-8-sig`, like the user config and the
  project's `.runledger.json` already were. A file that is still not valid UTF-8 (or not valid
  JSON) is ignored with the same warning as before.
- **Claude Code and Codex discovery resolve one home directory (`runledger.parser`,
  `runledger.adapters.codex`):** with `CLAUDE_CONFIG_DIR` and `CODEX_HOME` unset, session
  discovery used Python's home (`USERPROFILE` on Windows) while `guard install --global` and
  `runledger doctor` used `HOME`, so `runledger list` and `runledger receipt` read a different
  agent folder than the one a global install wrote to and the doctor named. Both now resolve the
  directory through the same helper the Droid adapter uses - the user home is `HOME`, then
  `USERPROFILE`, then Python's own home - which `docs/agents.md` documents.
- **`runledger doctor` inspects what the agents actually run (`runledger.doctor`):** the self-test
  no longer executes a foreign PreToolUse hook. A project that stores its own hook (an `echo hi`
  preflight, say) but nothing of RunLedger's is neither run nor reported as a RunLedger failure,
  and that hook no longer hides a genuine global install: doctor keeps looking through the scopes
  and self-tests the RunLedger entry it finds. Ownership is decided per command, not per file: a
  foreign PreToolUse command stored beside a RunLedger checkpoint (a project guard entry that was
  deleted or replaced) is reported - the `guard.hooks` entry warns and names it - and never run,
  and it cannot shadow the real global guard. A guard command that was hand-edited into something
  else is reported as the `fail` (exit 2) it is, with the reason the command was not run, instead
  of being executed to observe it. For Codex, the `commandWindows` copy - the command Codex runs on
  Windows - is the value doctor inspects and self-tests, so a stale or broken override is a `fail`
  even when the plain `command` is healthy (on the other platforms `command` stays the one that
  counts); an override RunLedger does not own is likewise reported, never run. The hook-timeout
  comparison uses the project's `guard.approval.timeout_s` for a global hook as well, because that
  hook reads the project policy when it runs: a 135 s global hook in a project that sets
  `timeout_s: 300` is now a `warn` (exit 1) instead of `ok`.
- **`doctor` fails cleanly on a broken config or approval URL (`runledger.doctor`):** a config file
  that is empty or holds only whitespace is a `config` failure (exit 2) instead of a silently valid
  empty file, and a config that is not UTF-8 JSON - a UTF-16 `~/.runledger/config.json` written by
  an ordinary PowerShell redirection - names the encoding instead of ending the run in an uncaught
  `UnicodeDecodeError`. A malformed approval server URL such as `approver.example` (no scheme) is
  a `warn` that names the unusable server and the `on_timeout` fallback, in the text report, in
  `--json` and in `guard status`, instead of aborting the whole report with a generic error.
- **A tracked `guard.log` warns even after an ignore rule (`runledger.doctor`):** the `log` check
  now asks the Git index as well, so a `guard.log` that was committed before `.runledger/` was
  added to `.gitignore` is reported as tracked - its command summaries stay in the repository and
  would be published - with `git rm --cached` as the fix. `git check-ignore` alone reported that
  file as safely ignored. An untracked log under an ignore rule stays `ok`.
- **Droid discovery and global installation resolve one Factory directory (`runledger.adapters.droid`):**
  with `FACTORY_HOME` and `FACTORY_HOME_OVERRIDE` unset, session discovery used Python's home
  (`USERPROFILE` on Windows) while `guard install --global --agent droid` and `runledger doctor`
  used `HOME`, so a customized `HOME` was handed a global hook outside the folder `runledger list`
  and `runledger receipt` read. All three now resolve the directory through the same helper - the
  user home is `HOME`, then `USERPROFILE`, then Python's own home - which `docs/agents.md` documents.
- **Droid connector names match the canonical `tool_rules` (`runledger.risk`):** a `tool_rules`
  entry written as `mcp__supabase__execute_query` or `mcp__vercel__*` now also decides the bare
  `supabase__execute_query` / `vercel__get_project` events Droid reports - the spelling a receipt
  already reads - so the live guard and a receipt agree on the same call. A rule written in the
  bare spelling keeps working, and the log still records the agent's own tool name
  (`docs/guard.md`).
- **Codex runs the installed hook on Windows (`runledger.guard`):** Codex starts a hook command
  itself instead of handing the string to a shell, and the `"<python>" -m runledger guard --agent
  codex` form never started there - the quotes were read as part of the program name - so a real
  Codex session left `guard.log` empty and created no lifecycle checkpoint. Every Codex entry now
  also stores `commandWindows`, the same command as `cmd /c "<command>"`, the copy Codex reads on
  Windows: for the guard hook and for both checkpoint hooks. `install` writes it, repairs an entry
  that lacks or has a stale one in place, and leaves the files of Claude Code and Droid (whose own
  hook runners do start the quoted path) unchanged. Measured with codex 0.163.0: the destructive
  command is denied by the hook, a benign one runs, and `SessionStart`/`UserPromptSubmit` create
  their checkpoints (`docs/guard.md`).
- **Droid and Codex guard decisions reach their receipt (`runledger.guard_receipt`):** records are
  linked in two passes - every record whose `tool_use_id` the transcript knows takes that step
  first, then the rest match the logged tool and summary in transcript order. That second pass is
  what links a Droid call (its payload carries no id, and the log tool `Execute` faces a `Bash`
  step), a Codex call (the hook's `exec-...` id is not the rollout's `call_id`) and a legacy
  no-id row, using the canonical tool name and the `Bash`/`PowerShell` pair the adapters choose
  between. The equivalent shell queues are compared by transcript position, not by tool name, so a
  command repeated across a shell switch (the same `Get-Content .env` run through PowerShell, then
  Bash) keeps its blocked call on the first step and its approved retry on the second. Rows without
  an `agent` (v0.6.0) keep matching their tool name exactly, and a decision
  that fits no step stays visible in the receipt as unmatched.
- **A Codex code-mode `exec` script becomes the shell step it ran (`runledger.adapters.codex`):**
  current Codex versions send shell calls as a `custom_tool_call` named `exec` whose input is
  JavaScript around `tools.exec_command({cmd: ..., shell: ...})`. RunLedger read that as one
  nameless `exec` step, so the command was invisible to the risk rules and the guard's decision on
  it had no step to link to. Each call inside the script is now one `Bash` or `PowerShell` step
  carrying its `cmd`, and a code-mode result's text blocks are shown as their text instead of raw
  JSON. Calls are located in the JavaScript itself, so the same text in a comment, in a string or
  in a regular-expression literal is an example, never a step: the quote in `const r = /\"/;`
  cannot hide the call that follows, and the division in
  `n++ / (await tools.exec_command({...})).exit_code / 2` is not read as a pattern. Telling the two
  apart is the one part of the reading that stays uncertain, so it no longer guesses: a script
  carrying any `/` that opens neither `//` nor `/*` is searched as raw text instead, and the call
  it names is reported even when it was written in a comment - a reported call beats a hidden one.
  `cmd`, `command` and `shell`/`program` values are read from double-quoted, single-quoted and
  static template literals, while a template that interpolates a variable leaves the script as its
  single step under `exec`; an unterminated string, template or comment is searched as raw text
  the same way.
- **A quoted SQL name is an identifier (`runledger.mcp_risk`):** single-quoted string literals are
  still blanked before the statement is matched, but a double-quoted or backtick name keeps an
  identifier placeholder. `DELETE FROM "users"` and `UPDATE "users" SET admin = true` are high
  like their unquoted forms, and `DELETE FROM "users" WHERE id = 1` stays medium: it used to be
  read as `DELETE FROM WHERE id = 1` and graded high.
- **A separator ends a delete option (`runledger.risk`):** the 0.7.0 rules accepted only a space or
  the end of the command after a flag, so `git push origin feature --delete; echo done` (and the
  same with `&&`, `|` or a newline, and `-d`, `git branch -D`, `git branch --delete --force`)
  carried only the generic medium `git push` finding or none at all. A shell separator now ends the
  option list, so the high `Deleted a remote git branch or tag` finding fires there too. The
  older flag rules had the same gap: `rm ./x -rf; echo done` and
  `git push origin main --force; echo done` now carry the finding their end-of-command forms had.
- **A `find -name` pattern is not a command (`runledger.risk`):** find's pattern options are read
  as text in the blanked view, so `find . -name 'helm uninstall web' -print` no longer produces the
  high `Uninstalled a Helm release` finding - the same for any other destructive name in a `-name`,
  `-path`, `-iname` or related pattern. A real command in the expression, such as
  `find . -name x -exec helm uninstall web \;`, still matches. Only a pattern whose own text holds
  no substitution syntax is blanked: a word carrying `$(` anywhere, or a backtick for a POSIX shell,
  is kept whole as the code it is, so the shell runs it before `find` starts and
  `find . -name "$(rm -rf ./victim)"`, ``find . -name `helm uninstall web` `` and a pattern with a
  nested or escaped backtick (``find . -name "`echo \`rm -rf ./victim\``"``) are all denied. No
  quote state is read at all - quoting and escapes are not resolved - so a quoted region can never
  cover a substitution the shell really runs: the escaped double quote in
  `find . -name \"'"'"$(rm -rf ./victim)" -print` no longer desynchronises the reading and hides the
  deletion. The reading is deliberately coarse, and the price is a finding for text that never
  runs: a `$(` or backtick in single-quoted or escaped text is read as code too, so
  `find . -name '$(rm -rf ./victim)' -print` is denied as well, although the shell substitutes
  nothing there.
- **A command rule that can backtrack catastrophically is dropped (`runledger.secrets`):** the
  safe-regex check rejects a repeat of any kind inside a repeat (`(?:a{1,32}){1,32}b`,
  `(?:a?){40}b`, `(a+)+`) and an alternation inside one (`(a|a)*b`, `(a|aa)+b`), so a user or
  project `command_rules` entry (or an `extra_secret_paths` pattern) with one is dropped with the
  existing `Dropped invalid command_rules.deny pattern ...` warning instead of stalling the guard
  hook or a receipt. A user or project rule also reads at most the first 20 000 characters of a
  command, like the SQL inspection, so no rule can make the synchronous guard path walk an
  arbitrarily long command.
- **The `log` check is `info` outside a Git work tree (`runledger.doctor`):** the ignore rule can
  only be verified inside a Git repository, so an existing `.runledger/guard.log` in a plain folder
  no longer prints an `ok` line that reads as if the rule had been checked. The line still carries
  the log's size, its last decision time and its 7-day error count, never its contents, and the
  status change does not touch the exit code; recent error rows still warn (fail-open), and every
  other status is unchanged - ignored `ok`, not ignored `warn`, tracked `warn`, no log `info`.
- **`docs/doctor.md` shows the report the CLI really prints (`docs/doctor.md`):** the text samples
  no longer label an uninstalled hook `[ok]` (the CLI reports it as `info`), they use the check
  titles the text report prints (`guard log`, not `log`), and the numbers in the sample summary and
  in the `--json` example are the ones that run prints. The `log` section documents the outside-Git
  `info` status.

## [0.6.1] - 2026-10-18

### Changed

- **A Droid run with no recorded usage no longer prices its models (`runledger.client`,
  `runledger.server`):** the push body and the stored run keep `cost_usd` `null` on every model of
  such a run instead of the `0.0` the priced-model bucket used to carry, so a model group made only
  of missing-usage runs shows `cost_usd` `null` with `priced_runs` 0 in `GET /api/stats` and
  `GET /api/insights` (n/a in the dashboard), not a free `$0.00`.
- **Insights agent aggregates carry their lower-bound flag (`runledger.server`):** `agents[]` in
  `GET /api/insights` now returns `priced_runs`, `partial_runs` and `cost_is_lower_bound`, so an agent
  whose runs are all only partly priced reads `≥ $X` in the dashboard instead of a complete figure.
- **The compliance report marks a partly priced developer as a lower bound (`runledger.server`):** a
  developer row in `GET /api/export/report.html` reads `≥ $X` when any of that developer's runs is
  only partly priced, matching the team figure above it (previously only a run with no cost at all
  produced a mark).
- **The dashboard budget card shows `n/a` for an unpriced month and `≥ $X` for a partly priced one
  (`runledger.server`):** a month whose runs all have unknown costs no longer reads `$0.00 spent`, and
  a month with partly priced runs is marked `≥`, in both the team figure and the developer rows. The
  note next to the figure names how many runs it leaves out. The numeric spend behind the alert
  thresholds and the progress bar is unchanged.
- **Quality score: an install word inside a test argument no longer hides the run
  (`runledger.quality`):** an install verb (`install`, `uninstall`, `add`, `i`, `remove`)
  disqualifies a segment only when it is a package manager's subcommand in the command word or
  wrapper chain (`pip install pytest`, `python -m pip install pytest`, `uv pip install`, `uv add`,
  `poetry add`, `npm install`, `npm i`, `pnpm add`, `yarn add`, `bun add`). Real runs whose
  arguments contain those words earn their `tests_run` credit again, as they did in v0.6.0:
  `pytest -k add`, `pytest -k "install and not slow"`, `npm test -- --grep install` and
  `jest -t uninstall` are runs. Inspection flags (`--version`, `--help`, `--collect-only`,
  `--list-tests` and the rest) and every other install and inspection case behave as before.

- **Text sent to Claude masks e-mail addresses and home folders, with no opt-out
  (`runledger.llm`, `runledger.review`, `runledger.summarize`):** every request body, from `--ai`
  summaries as well as from `--review`, replaces e-mail addresses with `[EMAIL]` and the user name of
  `C:\Users\<name>`, `/Users/<name>` and `/home/<name>` with `~`, on top of the secret patterns that
  were already applied. Masking happens before the text is cut to its limit, so a home folder is
  never sent in half, and it applies to the whole message at the API boundary. A session mentioning
  `ana@example.com` or `C:\Users\ana\app\x.py` now sends `[EMAIL]` and `~\app\x.py`; commands keep
  their shape. A review's data block also escapes `<` as `\u003c`, so a command containing
  `</run_data>` cannot close it. `docs/analysis.md`, `docs/quickstart.md` and the README state what
  is masked.
- **A home folder whose user name contains spaces or punctuation is masked whole, and nested step
  inputs are masked before `--ai` writes them as JSON (`runledger.llm`, `runledger.summarize`):** a
  quoted path such as `"C:\Users\Jane Smith\app\x.py"` or `/Users/Jane Smith/app/x` was masked only
  up to the first word, leaving ` Smith` in the text sent to Anthropic; the user name is now consumed
  to the separator or quote, so both read `"~\app\x.py"` and `~/app/x`. Inside quotes the name runs
  to the next path separator, so a folder named `Jane (Smith)` goes whole
  (`"C:\Users\Jane (Smith)\app\x.py"` sends `"~\app\x.py"`), and the same name is masked when it
  appears unquoted. A step input is masked at every level before serialization, so a `MultiEdit`'s
  `edits` (and the doubled Windows separators that JSON produces) can no longer carry a complete
  home path into an `--ai` request. The check at the API boundary also recognizes a path whose
  separators were doubled, and command boundaries are kept: `cat ~/app/x && ls` still sends `&& ls`.
- **The dashboard reads a second, read-only connection, and a backup no longer stops writes
  (`runledger.server`):** reports, stats, insights, the run list and the exports read from their own
  read-only connection (`file:...?mode=ro`), so a long report or a `runledger backup` running beside
  the server cannot hold the write lock and block pushed runs. `runledger backup` copies through that
  connection in pages with a short sleep, so it stays cheap on a busy store. Test databases that live
  in memory keep their single connection. A read never sees uncommitted rows, which is what the
  approval decision flow needs.
- **Dashboard: unknown costs show `n/a`, partial costs show `≥$X`, and the unpriced hint names the
  real cause (`runledger.server`):** a run with no recorded token usage or no known model price shows
  `n/a` (never `$0.00`), with a tooltip saying token usage was not recorded when that is the reason,
  and a figure that leaves unpriced tokens out is prefixed with `≥` in the run list, the KPI card and
  the by-user, by-project, by-agent, by-model, insights and cost-per-run cells. The hint under the cost
  KPI now splits the unpriced runs into those without recorded usage and those with an unknown model
  price instead of always blaming the price.
- **Dashboard filters: Max quality and AI verdict, kept in the URL (`runledger.server`):** the filter
  bar next to Min quality has a Max quality number input and an AI verdict select
  (`looks_safe`, `needs_review`, `dangerous`, or any), and the whole filter set (developer, project,
  agent, min risk, min quality, max quality, verdict) is written to the URL hash and restored from it,
  so a reload or a shared link shows the same runs. The filter bar belongs to the Runs view: switching
  to Insights, Approvals, Keys, Audit or Settings leaves it untouched, and returning to Runs writes
  the filters back into the hash, so a tab switch no longer loses what the developer typed (and a
  filter edit landing after the switch cannot pull the page back to Runs).
- **Dashboard polling pauses while the tab is hidden and backs off after a failure
  (`runledger.server`):** the pending-approvals poll on the dashboard and the poll on the individual
  approval page now stop while the page is hidden, refresh at once when it becomes visible again, and
  grow the delay from 3 s to 6 s, 12 s and so on up to 30 s after a failed request (a 429 included)
  instead of hammering a server that is down. The first success returns to the 3 s delay.
- **Grade E is gone from the quality distribution (`runledger.server`):** `GET /api/insights` returns
  the `quality.by_grade` buckets A, B, C, D and F, the dashboard shows five buckets and no E badge, and
  a run stored earlier with grade E keeps its score but shows no grade badge.
- **Dashboard comments and the run detail panel match the real page (`runledger.server`):** the header
  comment no longer claims features hide on 404/403 because endpoints may not exist (only admin-only
  panels hide, on 403), and a run row opens a detail dialog that names the developer, the key that
  pushed the run ("Pushed by alice-laptop"), the cost, risk, quality, AI verdict, steps, tokens and
  revision, with a link to the receipt.
- **Project `.runledger.json` is tighten-only in receipts as well as the guard:** `ignore` and
  lowering `severity_overrides` are read only from your user config (`~/.runledger/config.json` or
  `RUNLEDGER_CONFIG`). A project `.runledger.json` may raise severities and add safe
  `extra_secret_paths`; if it tries to ignore or lower risk kinds, the receipt keeps the findings
  and adds a note.
- **`runledger guard install` writes interpreter-bound hook commands by default:** `guard install`
  writes `"<sys.executable>" -m runledger guard` and `"<sys.executable>" -m runledger checkpoint --hook`
  so the hooks run without `runledger` on `PATH` and when the Python path contains spaces, upgrades
  earlier bare `runledger ...` hook entries in place, and verifies the configured command exits `0`
  on empty JSON input, run from the project folder with your environment unchanged (a command that
  cannot start RunLedger there is reported as a warning, not as a success). Pass `--portable` to
  write bare `runledger ...` commands for checked-in settings files.
- **HTML receipts inside a Git work tree are safe by default, and default receipt filenames
  auto-increment (`runledger.cli`):** `runledger receipt` now enables safe mode by default for HTML
  files written inside a Git repository (matching Markdown and JSON; pass `--no-safe` for full
  paths), and when `-o` is omitted it writes `runledger-<stem>-2.<ext>`, `runledger-<stem>-3.<ext>`,
  and so on instead of overwriting an existing receipt file.
- **The dashboard sign-in form replaces the key-in-URL instructions (`runledger.server`):** the `401`
  sign-in page now shows a form (`method="post"`, action `/session`) instead of teaching
  `/?key=YOUR_API_KEY`, and a wrong key answers the same page with an error message. Docs
  (`docs/api.md`, `docs/enterprise.md`, `docs/self-hosting.md`, `docs/quickstart.md`) describe the
  form, the Strict cookie, the confirmation step and the session cap; the `?key=` link is documented
  as deprecated.
- **The Docker image identifies itself, and the Compose stack is hardened (`Dockerfile`, `deploy/`):**
  the image carries the full OCI label set, with `org.opencontainers.image.version` and
  `org.opencontainers.image.revision` fed by the `RUNLEDGER_VERSION` and `RUNLEDGER_REVISION` build
  arguments (the CI docker job passes the version from `pyproject.toml` and the commit it built, then
  checks the labels on the built image); the base image stays on a tag, so Dependabot can bump it.
  `deploy/docker-compose.yml` runs the server with a read-only root filesystem (only the data volume
  and `/tmp` are writable), drops every Linux capability, forbids gaining new ones, pins its network
  to `172.28.0.0/16` and uses that subnet as the default `RUNLEDGER_TRUSTED_PROXIES`, so every user
  keeps their own failed-sign-in counter and audit address without any configuration; Caddy is pinned
  to `caddy:2.8` and waits for the server's healthcheck before it starts. `deploy/backup.sh` uses
  POSIX commands only, copies exactly the file the backup command wrote, and gives the host copy a
  free name, so two runs within the same second neither overwrite nor miss each other. `deploy/.env.example`
  documents the new trusted-proxies default.

### Fixed

- **The release workflow installs the package before it runs the tests
  (`.github/workflows/release.yml`):** the `Run tests` step installed pytest only, so every test
  importing `runledger` failed when the v0.6.1 tag was pushed and the run stopped before it built
  or published anything; the step now installs the package exactly like the CI test job
  (`python -m pip install -e . pytest`), and `tests/test_packaging.py` pins that so the drift
  cannot come back.
- **The stored receipt of a run with no recorded usage no longer carries a `$0.00` cost
  (`runledger.server`):** the run row, the list and the aggregates already read `n/a`, but
  `GET /api/runs/{id}` still served the client's original `cost_usd` `0.0` inside the embedded
  `receipt` (`receipt.totals` and every `receipt.models` bucket), which read as a free run. The
  server now normalizes the receipt it stores and serves the same way it normalizes the flattened
  run: a run whose `usage_source` is `missing` has `cost_usd` `null` in its receipt too, including
  a row an older server stored with the zero. A run that reports its usage keeps its figure, so a
  reported `$0.00` stays `0.0`, and a receipt from a client that reports no `usage_source` is
  untouched.
- **`GET /api/stats` reports `unpriced_runs` and `cost_is_lower_bound` on `by_model` rows
  (`runledger.server`, `docs/api.md`):** the model rows were the only group rows without the two
  fields the documentation's example already showed, so a consumer could not tell that a model's
  figure leaves unpriced runs out. A model bucket with no price and a run that is only partly
  priced both count as unpriced, exactly as they do in the developer, project and agent rows.
- **Quality score: a bare pytest count line counts as a pass again (`runledger.quality`):** a
  footer without the timing pytest adds (`3 passed`, `3 passed, 1 skipped`, `=== 3 passed ===`,
  with or without a service-name or timestamp prefix) was read as unreadable, so the run lost the
  15 `test_outcome` points that v0.6.0 awarded it. The timing (`in 0.20s`) is optional again;
  unreadable output (`Done.`) and counts inside free-form log text (`retried after 3 errors in
  0.10s`, `lint found 7 errors`) stay unknown.
- **`docs/github.md` shows the workflow's `permissions:` block (`docs/github.md`):** the page
  described the `pull-requests: write` grant in prose and linked the example workflow; the
  workflow snippet now carries the block itself (`contents: read`, `pull-requests: write`).

- **Safe mode masks an e-mail address that ends a sentence (`runledger.safe`):** an address
  followed by a full stop was left as it was typed, so a prompt such as `contact me@example.com.`
  reached the Markdown, JSON and HTML receipts, the body of `push --safe`, the stored run and the
  served run page with the address in it, while the same address followed by a space was masked.
  The address now ends at its last label, so the punctuation after it stays in the text
  (`contact [EMAIL].`) and an address followed by a comma, semicolon, colon, question or
  exclamation mark, or a closing bracket or quote, is masked in the same way. The same matcher at
  the Claude API boundary already masked these addresses and is unchanged.

- **The pull request comment keeps an SSH remote byte-for-byte (`runledger.github`):** every `@` in
  session text was split with an invisible separator, which changed a remote such as
  `git@github.com:org/repo.git` in the comment even though GitHub does not read an at-sign inside a
  word as a mention. The separator is now used only where GitHub would read one (at the start of the
  text or after a character that is not a letter, digit or underscore), so a remote and an address
  in a session's text are shown exactly as they were written, while `@someone` after a space or an
  opening bracket is still not a live mention.

- **The README's safe-mode sentence matches the code (`README.md`):** it promised that "HTML
  receipts keep full paths", while `runledger.cli.default_safe` writes every format safe by default
  inside a Git repository, HTML included, and only Markdown and JSON to stdout are always safe. The
  page now says the same as `docs/quickstart.md`: a receipt written outside a repository keeps full
  paths unless `--safe` is passed.

- **A test summary behind a log prefix is read again (`runledger.quality`):** a pytest footer
  written by a container, a wrapper or a log stream keeps its counts when it carries a
  docker-compose or container service name (`app  | 3 passed in 0.5s`, `web-1  | 1 failed, 2 passed
  in 0.4s`) or a timestamp (`2026-10-18T12:00:00Z 3 passed in 0.5s`, `[12:00:01] 3 passed in
  0.5s`), alone or together, so a passing suite behind a prefix is no longer unknown. Only those
  prefixes are allowed, and only at the start of the line: a count in free-form text
  (`retried after 3 errors in 0.10s`, `app  | done, 3 passed in 0.5s`) is still not a summary.

- **A very long line of digits no longer stalls the quality score (`runledger.quality`):** the
  count fallbacks tried a 20 KB run of digits from every digit before giving up, which took over a
  second per line; each count is now anchored to its start, so the same line parses in about a
  millisecond. A 20 KB line of any shape (digits, `=`, spaces or a near-match footer) stays well
  under a tenth of a second.

- **A bare URL in session text no longer becomes a live link in the pull request comment
  (`runledger.github`):** GitHub links a bare `https://…` or `www.…` by itself, so escaping the
  Markdown punctuation around it was not enough: a request such as `See https://evil.example/x`
  rendered as a clickable address in a comment that everyone who can read the pull request can
  see. The comment now carries an invisible separator inside the scheme and after `www.`, so the
  address stays readable while the footer's link to `https://runledger.site` is the only live link
  left in the comment.

- **The tests line reports the session's final test invocation (`runledger.github`):** a final test
  command whose captured output has no readable summary is reported as `last run unreadable (…) ·
  verdict: unknown` instead of borrowing an earlier run's pass; a readable earlier run is named as
  earlier (`An earlier readable run passed (3 passed, 0 failed).`) and never as the last run. A
  final run that passed or failed reads as before.

- **Test runs are read from PowerShell call-operator commands and from real summary lines only
  (`runledger.quality`):** `& pytest -q` and `& "C:\Python312\python.exe" -m pytest -q` are runs
  again, with their outcome at score level, and a quoted executable keeps its spaces, so
  `& "C:\Program Files\Python312\python.exe" -m pytest -q` counts too, while a literal mention
  (`echo pytest`, `& echo pytest`, `echo "C:\Program Files\Python312\python.exe" -m pytest`) still
  earns nothing. A line counts as a runner summary only when it has a summary shape (counts and a
  timing and nothing else), so `retried after 3 errors in 0.10s` no longer turns a following
  `3 passed in 0.20s` into a failure, and free-text error counts stay ignored. Pytest's own elapsed
  suffix is part of that shape: `==== 3 passed in 75.12s (0:01:15) ====` parses again. The v0.6.0
  fallback for an explicit failed count is back: `2 failed, 3 passed` without a timing marks the run
  failed even when the step reports no error, while unreadable output (`Done.`) stays unknown.

- **A failed Opus escalation keeps the first review (`runledger.review`):** when
  `RUNLEDGER_ESCALATE_OPUS=1` re-runs a `dangerous` verdict on Opus and that call fails, the receipt
  keeps the Sonnet review instead of losing it and printing "AI risk review skipped".
  `ai_review.notes` records one line starting with `escalation failed:` and `ai_review.escalated` is
  `false` (`true` after an escalation that succeeded); both keys are new.

- **`--ai` reads its summaries from a reply that is not pure JSON (`runledger.summarize`):** the
  first JSON object in the reply is decoded on its own, so trailing prose (even with braces in it)
  and a ```json fence no longer lose the summaries. Step entries that are not objects, that name no
  usable step number (an int, `"2"` and `two` all work) or that carry no text are skipped instead of
  raising, and a reply with no JSON at all still prints `AI summaries skipped: ...` and keeps the
  rule-based receipt.

- **No model-downgrade estimate without a Haiku price (`runledger.advisor`):** the
  `model_downgrade` recommendation is built against the price of `claude-haiku-5-5`; a user price
  table that has no Haiku row now yields no downgrade advice instead of relying on the estimate
  silently collapsing to zero.

- **HTTP error bodies are closed in every client (`runledger._http`, `runledger.llm`,
  `runledger.client`, `runledger.github`):** a 4xx/5xx reply from the Claude API, the team server or
  GitHub is read through one helper that closes the response, so `-W error::ResourceWarning` runs
  clean and a long-lived process does not keep the socket. The error text the user sees is unchanged.

- **Quality score: installing or inspecting a runner is no longer a test run, and an unreadable
  result is unknown instead of a pass (`runledger.quality`, `runledger.risk`, `runledger.parser`):**
  each `&&`/`;`/`|`/newline segment is judged by the command word it starts with, after `FOO=1`
  assignments and wrappers (`uv run`, `poetry run`, `npx`, `python -m`) are stripped, so
  `pip install pytest`, `npm install --save-dev jest` and `python -m pip install pytest` earn no
  `tests_run` credit and no pass outcome, while `uv run pytest`, `cd app && pytest`, `pytest | tee`,
  `npx jest`, `npm test`, `go test`, `cargo test` and `dotnet test` still count (a path such as
  `tests/test_install.py` never disqualifies a real run). `pytest --version`, `--help`,
  `--collect-only`, `--co` and `jest --list-tests` are inspections, not runs. Counts are read only
  from a runner's summary lines (the pytest footer or quiet form, the jest/vitest `Tests:` line,
  mocha's `N passing`/`N failing`, go's `ok`/`FAIL` lines, cargo's `test result:` line, unittest's
  `Ran N tests` footer), so "retried after 3 errors" in log text is not a failure, `0 errors` is
  not one either, and a run whose output has no readable summary is unknown: it earns neither the
  +15 nor the -15 instead of counting as a pass. The test-file diff verdicts are computed once per
  receipt and shared with the risk rules, and a file rewritten five or more times through the shell
  (`echo x >> a.py`, `sed -i`, `Set-Content`) now counts toward `retry_loops`, as PowerShell writes
  already count toward the changed-file list. Sessions that only installed a runner score up to 25
  points lower than before.

- **Secrets in assignments whose name only contains a keyword (`runledger.secrets`,
  `runledger.guard`):** `MYTOKEN=abc123`, `TOKEN2=abc123` and `MY_ACCESS_TOKEN2=abc123` are masked
  again in `guard.log`, receipts and the text sent to an LLM, as they were in v0.6.0. The redaction
  patterns accept a bounded identifier prefix and suffix around the keyword
  (`[A-Za-z0-9_.-]{0,40}` on each side), so a long name without a keyword cannot cause
  backtracking; variable references, placeholders, all-caps names and digit-only values stay
  readable. Detection (`find_secrets`, and the risk findings built on it) keeps its word-anchored
  12-character threshold.

- **Guard hook ownership with versioned or free-threaded interpreters (`runledger.guard`,
  `runledger.rollback`):** `guard install` stores the literal `sys.executable`, so the recognizers
  now accept every interpreter name it can write, including `python3.13t.exe`, `python3.14t`,
  `pythonw.exe` and `pypy3`. Installing again updates the existing entry instead of adding a second
  one, `guard uninstall` removes the guard and both lifecycle checkpoint hooks, and the install
  self-test recognises the stored command instead of warning that it is not a RunLedger guard
  command. Foreign hooks (`echo runledger guard`, `other.sh && runledger guard`, another
  executable) are still left alone.

- **Spaced drive paths are a label output shows, never an input to a decision
  (`runledger.paths`, `runledger.risk`, `runledger.summarize`, `runledger.receipt`,
  `runledger.safe`, `runledger.guard`, `runledger.labels`):** an unquoted path that contains
  spaces (`echo hi > E:\Work\Client y\z.txt`) is two words to a shell, and findings, their
  de-duplication, severities, user `ignore` rules, guard rules and decisions, file keys,
  deletion state, counts and Git lookups are computed from exactly those words. Reading the
  whole folder name (`E:\Work\Client X\secret.txt`) happens when a receipt, a push payload or a
  guard message is rendered, so a finding shows the whole path and a safe receipt shows
  `…/Client X/secret.txt` instead of the unmasked `E:\Work\Client`, while the files a receipt
  lists stay the files the shell wrote (`touch C:/repo/My Folder/x.txt` writes `My` and
  `Folder/x.txt`). A quoted path (`"E:\Work\Client X\secret.txt"`) is one word already and is
  left as written. A label can therefore no longer merge two findings or hide one:
  `cat E:/Work/Client y/z.txt; rm 'E:/Work/Client y/z.txt'` keeps the low read and the high
  outside delete and the default guard asks again, a user `{"ignore": ["y/z.txt"]}` cannot hide
  the delete of `E:/Work/Client`, and a guard `reason:` rule matches the raw reason
  (`reason:(e:/work/client)` still denies it).

- **Findings beside a spaced path are the shell's own reading again (`runledger.risk`):** the
  joined/unjoined "union" this milestone added between the two readings is gone, which restores
  three behaviours that join-based analysis had lost: with the working folder `C:/repo`,
  `C:/repo/bin/rm sub/echo ../outside.txt` reports the high outside-the-working-folder finding for
  `../outside.txt` and the default guard asks instead of allowing the outside delete;
  `rm C:/elsewhere/tests/test_a.py other/file.txt` keeps the high `test_deleted` finding for
  `test_a.py` and `rm C:/elsewhere/key.pem other/file.txt` keeps the high `secret_file` finding
  for `key.pem`; and `echo hi > C:/repo/migrations/001_init.sql other/file.txt` reports the medium
  `db_migration` for the real redirect target. `rm C:/repo/a.txt ../outside.txt`,
  `rm C:\a b\x nested/../../outside.txt` and their `>` and `<` redirect forms still report the
  high finding for the operand that climbs out of the project and still ask again.

- **A queued heredoc starts at the newline that ends the command line (`runledger.shell_syntax`):**
  a newline inside a multiline `$'...'` or `"..."` string is part of that word, so a pending
  heredoc keeps waiting for the command's real terminator. In `echo <<EOF $'first` / `second'` /
  `payload` / `EOF` / `rm -rf ./build` (five lines, as Bash runs them) the heredoc body is now
  `payload` and the `rm -rf` after it is read again: the receipt keeps the high
  recursive-force-delete command finding and the default guard denies it, instead of treating the
  delete as quoted heredoc data.

- **Hook ownership reads the whole command, and follows the interpreter that is running
  (`runledger.paths`, `runledger.guard`, `runledger.rollback`):** the command is rejected as
  foreign before any basename is read when it holds an unquoted shell operator, redirection,
  command substitution, sub-shell or second line, so `true;/usr/bin/python3.13t -m runledger
  checkpoint --hook` (and the `guard` form, with `&&`, `||`, `|`, `&`, `>` or a second line) is
  preserved again: `guard install` appends beside it and `guard uninstall` leaves it, as v0.6.0
  did. A `$` or a backtick in the executable word keeps the entry foreign as well, even inside
  double quotes, because a POSIX shell executes those substitutions:
  `"/usr/$(printf bin)/python3.13t" -m runledger checkpoint --hook` and its backtick form (and
  the `guard` forms) compute their own path, so they are left untouched instead of being replaced
  or removed. A single command is also ours when its executable word is the interpreter that runs
  `install`, which writes that path literally: a copied or renamed interpreter
  (`agent-python.exe`) now works through install, the no-op second install, the install self-test
  and uninstall. Other interpreters still match the python/pypy name pattern
  (`python3.13t.exe`, `pythonw.exe`, `pypy3`).

- **A completed backup is never overwritten by a delayed run (`runledger.cli`):** the backup
  sequence is read before the name marker exists, so a run that paused there could claim a number
  another run had since used, finished and released, and the atomic rename then replaced that
  finished copy. The marker claim now checks the final target as well and moves to the next suffix
  when it is taken, so overlapping backups in the same second still leave one copy per run.

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
  `adapters()` reports a broken adapter import once per process on `stderr` (or re-raises when
  `RUNLEDGER_DEBUG=1`).
- **Guard project root resolution and subfolder support (`runledger.guard`, `runledger.guard_receipt`, `runledger.risk`):**
  the guard resolves the project root from `CLAUDE_PROJECT_DIR` / `FACTORY_PROJECT_DIR` /
  `DROID_PROJECT_DIR`, else the nearest ancestor of the event's `cwd` (including the `cwd` itself)
  that contains `.runledger.json` or `.git`, else `cwd`. Policy
  is read from `<project_root>/.runledger.json`, `.runledger/guard.log` is written only at the
  project root, `write_outside` / `read_outside` / `shell_outside` use the project root as the
  boundary while resolving relative paths against the event's `cwd`, and receipts load both root
  and legacy subfolder guard logs with de-duplication. Running `runledger guard` interactively with
  a TTY stdin now prints help and exits `2` instead of blocking.

- **Guard roots, foreign hooks, and the install self-test (`runledger.guard`):** a project whose
  root holds only `.runledger.json` (no Git) now enforces its policy and writes one
  `.runledger/guard.log` at that root when the agent runs in a subfolder; `guard install` and
  `guard uninstall` recognise only the complete RunLedger commands, so a foreign hook that merely
  mentions the words (`echo runledger guard`, `other-check.sh && runledger guard`) is kept intact
  instead of being replaced or removed; `guard install` without `--project` writes
  `.claude/settings.json` at the project root (git top level or policy marker) instead of in the
  subfolder it was run from; and the install self-test runs the stored command from the project
  folder without adding the installer's checkout to `PYTHONPATH`, so a hook that cannot start
  RunLedger there is reported as a warning rather than verified.

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
- **ANSI-C strings do not start a heredoc (`runledger.shell_syntax`, `runledger.shell_writes`,
  `runledger.risk`):** `echo $'it\'s <<EOF'` is one word, because a backslash escapes the quote
  inside `$'...'`, so the commands on the following lines are analysed again in the guard and in
  receipts instead of being swallowed as a heredoc body. A literal newline is part of the string
  until its closing quote, so `echo $'first` / `second'` followed by `rm -rf ./build` still
  reports the recursive force delete (guard: deny).
- **A `#` comment ends at the newline (`runledger.shell_writes`):** the shell tokenizer no
  longer reads the line after a comment as arguments of the commented command, so
  `echo ok # <<EOF` followed by `rm k.txt` still lists `k.txt` as deleted, and `echo a#b` is
  one word.
- **A project severity override is compared with the finding it matches (`runledger.risk`):** a
  project `.runledger.json` still raises severities, but an override below the finding's actual
  severity (a `shell_outside: medium` override on a delete outside the project, for example) no
  longer lowers it, and the receipt counts the kind in its "not applied" note.
- **Cmdlets are only sniffed in command position (`runledger.ps_writes`):** a `Bash` step whose
  only cmdlet-looking text is quoted or commented (`echo 'Set-Content is a cmdlet'; touch t.txt;
  rm r.txt`) stays POSIX, so its writes and deletes are listed instead of being lost to the
  PowerShell extractor, while a real `Set-Content` command word is still analysed as PowerShell.
  Quoted text is blanked through its closing quote even across newlines, so a multiline quoted
  example (`echo "example:` / `Set-Content -Path demo.txt"` / `; touch t.txt`) cannot flip a
  POSIX step to PowerShell either.
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
- **Safe mode fully masks spaced paths and unknown user names (`runledger.safe`, `runledger.risk`):**
  a foreign drive path that contains spaces is masked as one path, quoted (`type "E:\Work\Client
  X\secret.txt"`) or bare, instead of surviving in the step text, and a folder name with several
  words (`E:\Work\Client X Corp\secret.txt`) is one path too, so it is trimmed to
  `…/Client X Corp/secret.txt`; an unknown home user name with
  spaces or non-ASCII characters (`/home/john smith`, `/Users/mary jane doe`,
  `C:\Users\José Smith\Documents`, including runs of spaces such as
  `C:\Users\John  Smith\Documents`) is consumed up to a separator, quote or end of line, so no
  surname fragment reaches a receipt; and the scp-remote exemption now needs a non-empty path after
  the colon, so a real address followed by a prose colon (`git@example.com: please respond`) is
  masked as `[EMAIL]` while `git@github.com:org/repo.git` is still kept. A shell command's unquoted
  drive path with spaces is read as one path, so the risk reason names the whole path (which safe
  mode can then mask) instead of a fragment.
- **Model normalization, verified OpenAI/Google prices, 1h cache writes and long-context tiers (`runledger.pricing`, `runledger/prices.json`):**
  `normalize_model` strips trailing `[...]` context tags (`claude-opus-4-6[1m]`), `custom:` and
  proxy suffixes (`custom:Claude-Opus-4.5-[Proxy]-0`), Bedrock/Vertex prefixes and `-latest` while
  keeping `-fast`, unverified `-preview` and `-chat-latest` unpriced. `prices.json` adds verified
  OpenAI (`gpt:6-astra`, `gpt:6.1-sol`, `gpt:6-luna`, `gpt:5.6-sol`, `gpt:5.6-cyber`,
  `gpt:5.3-codex`) and Google Gemini (`gemini:4-argon`, `gemini:3.8-flash`, `gemini:3.6-flash`,
  `gemini:3.5-flash-lite`, `gemini:3.1-flash-lite`, `gemini:3-flash-preview`, `gemini:2.5*`) list
  prices alongside all 20 v0.6.0 Anthropic keys, with backward-compatible optional fields
  (`cache_write_5m`, `cache_write_1h`, `long_context`, `valid_from`, `valid_until`, `windows`),
  once-per-process invalid price file warnings, and a 120-day staleness note in receipts.
  `parser._usage` reads `cache_creation.ephemeral_1h_input_tokens` and bills 1-hour cache writes at
  `2x` input (or `cache_write_1h`), and `cost_of` applies `long_context` rates per request (noting
  when Droid settings-only usage uses the base tier).
- **Long-context tiers, price windows and cumulative usage are enforced honestly (`runledger.pricing`, `runledger/prices.json`, `runledger.adapters.native`, `runledger.adapters.droid`):**
  the Haiku 5.5 row carries its documented `>100K` tier again (0.50/2.50/0.05, cache writes
  0.625/1.00), so a 150K-token request costs $0.0775 and two 75K requests cost $0.0155. `valid_from`
  /`valid_until` windows are evaluated against today's date in production lookups, and a user row
  outside its window is skipped so the bundled row stays in effect (a row past every window is
  `n/a`). Per-request tiering now follows the usage that carries request boundaries: native header
  totals keep their per-step requests, aggregate-only totals and Droid settings remainders are
  marked cumulative and stay on the base tier, and the receipt totals, `push` payload and advisor
  all price those buckets the same way. `PRICING_HELP` names the 120-day re-verification policy,
  and the bundled `checked` date is back to 2026-10-08 (its last recorded verification).
- **Codex, Claude Code and Aider usage and prompt parsing (`runledger.adapters.codex`, `runledger.parser`, `runledger.adapters.aider`):**
  Codex `token_count` events deduplicate repeated cumulative totals, fall back to
  `total - _prev_total` when `last_token_usage` is omitted, and handle counter resets; Claude Code
  `parse_session` streams in a single pass and retains user prompts starting with `<` (such as HTML
  or JSX snippets) while still filtering wrapper tags (`<command-`, `<local-command-`,
  `<system-reminder>`, etc.); and Aider parses `cache write` and `cache hit` token counts in either
  order without double-counting `sent` and distributes per-step integer remainders so step tokens
  sum to `totals.tokens`.
- **Backup names are reserved exclusively and stems stay unique per database (`runledger.cli`):**
  `runledger backup` reserves its `runledger-<stem>-YYYYmmdd-HHMMSS[-N].db` name with an exclusive
  marker create before copying, so two backups started in the same second take different names
  instead of one silently replacing the other, and a failed copy removes the reservation, still
  leaving no `.db` or `.tmp` file behind. The stem percent-encodes every character outside
  `A-Za-z0-9._-` instead of replacing it with `_`, so databases whose names differ only by such a
  character (`team a.db` and `team_a.db`) keep separate backup namespaces and retention for one
  never prunes the other database's copies.
- **Backup retention ignores unfinished copies (`runledger.cli`):** an unfinished backup is a
  `.lock` name reservation beside the target, never an empty `.db` placeholder, and retention
  counts only non-empty `.db` files. Overlapping `runledger backup --keep 1` runs can therefore no
  longer keep an unfinished copy while deleting the finished one (which also failed the run with
  "cannot find the file specified" when it printed its own result), and a copy that fails removes
  its own marker only, leaving every finished backup untouched. A zero-byte placeholder left by an
  interrupted older version is never counted and never pruned.
- **Droid sub-agent totals stay on the base tier (`runledger.adapters.droid`):** a child session's
  cumulative `inclusiveTokenUsage` is marked as a cumulative estimate before it is aggregated, so
  its usage cannot be billed at the long-context tier just because a genuinely long parent request
  shares its model bucket. A known 300K-token parent request next to a cumulative-only 500K-token
  child now costs $11 (tier 2 for the parent, base tier for the child) instead of $16, and the
  receipt, the advisor figures and the `push` payload all show that value.
- **A secret inside a spaced-path label is redacted everywhere the label is shown
  (`runledger.labels`, `runledger.guard`, `runledger.receipt`, `runledger.safe`):** the label that
  shows `E:/Work/Client TOKEN2=.../file.txt` as one path repeats text from the command, and
  applying it after the redaction of a risk reason put the raw assignment back into the risks the
  guard returns and posts to the approval server. Every render site now applies the label first
  and the redaction, the truncation and the safe masking on the labelled copy, so that command
  reads `(E:/Work/Client TOKEN2=[REDACTED])` in the guard's returned risks and reason, in the
  approval request, in receipts (Markdown, JSON, HTML) and in the pull-request comment. Decisions
  are unchanged - the shell's own words still decide - and the whole spaced path stays visible,
  with the assignment inside it masked.

- **An unknown cost is `null` (n/a), never `$0.00`, and a partly priced one is a lower bound
  (`runledger.server`, `runledger.pricing`):** a run whose agent recorded no usage keeps an unknown
  cost (`usage_source: "missing"`, `cost_usd: null`) instead of a free-looking `0`, and a Droid session
  without usage gets no cost in the receipt at all. Runs whose cost is only partly priced are counted
  as `partial_runs` and reported with a `≥` prefix: `GET /api/stats` totals and every breakdown carry
  `cost_usd` (`null` when nothing in the group is priced), `priced_runs`, `unpriced_runs` (null or
  partly priced) and `cost_is_lower_bound`, insights agents and models show `cost_usd: null` when no
  run of theirs is priced, `GET /api/budgets/status` counts the runs behind the month's spend and says
  when it is a lower bound, budget alerts name the unpriced runs ("lower bound: 2 of 30 runs
  unpriced"), the runs CSV leaves an unknown cost cell empty, and the compliance report reads `n/a`
  for a window with no priced run and `(lower bound)` with the counts otherwise. Markdown, HTML and
  pull-request-comment receipts carry the same `≥` mark on a partly priced total. A reported zero
  (`usage_source: "messages"` and `cost_usd: 0.0`) and a receipt from an older client keep their old
  meaning and still show `$0.00`. JSON consumers that assumed a number must handle `null` (see
  docs/api.md).

- **Retention can no longer delete the runs behind a budget alert, and audit events do not follow the
  runs' period (`runledger.server.maintenance`):** `RUNLEDGER_RETENTION_DAYS` below 35 is raised to 35
  with a warning on startup, because a shorter period deleted the current month's runs, dropped the
  month's spend and fired the same `budget.alert` threshold a second time. Budget alerts are purged by
  the calendar month they belong to (`month < cutoff month`), never by when they fired, so an alert of
  the current month survives any period. Audit events are deleted against
  `RUNLEDGER_AUDIT_RETENTION_DAYS` only (default 365 days while runs are pruned, `0` keeps them for
  ever), so a 35-day run period keeps a year of audit history instead of deleting it with the runs.
  With neither variable set, nothing is deleted, whatever its age, as in v0.6.0.

- **The pull request comment is only updated when it is RunLedger's own, and untrusted text can no
  longer build links (`runledger.github`):** the sticky comment is looked up among the comments the
  token itself wrote (`GET /user`; `GITHUB_TOKEN` cannot call it, so then only a bot's comment
  counts), and a comment planted by somebody else with the marker is left alone: a new comment is
  created instead of a `PATCH` to theirs. A refused `PATCH` (403 or 404, for example a comment from
  an app that has since been removed) falls back to a fresh comment with a notice on stderr. Session
  text (titles, risk reasons, models) is backslash-escaped for Markdown punctuation (`[`, `]`, `(`,
  `)`, `!`, backticks, `*`, `_`, `~`, `|`) and for line-leading `#`, `>`, `-`, `+` and `1.`, `#12`
  becomes `\#12`, HTML stays escaped and `@` is still neutralized with a zero-width space, so the only
  live link in the comment is the footer. A file path or command containing a backtick is shown in a
  code span whose fence is widened (`` `` dir/we`ird.py `` ``) instead of replacing the backtick with
  `'`.

- **The comment says which agent, what Guard decided and how the tests ended (`runledger.github`,
  `runledger.guard_receipt`):** each run's block now carries a `Guard:` line with the blocked, asked,
  approved and rejected counts plus the warnings (monitor-mode interventions and ignored loosening
  settings) and a `Tests:` line (`N test command(s) ran · last run 3 passed, 0 failed · verdict:
  passed`, or a distinct "no tests run"). The footer states that the comment was built in safe mode
  with paths shortened and e-mails masked, and the run table names Factory Droid like every other
  agent.

- **One unreadable session no longer loses the whole comment, and an empty folder can be accepted
  (`runledger.github`):** a session file that cannot be parsed is skipped with a
  `::warning file=…::` annotation naming it, the comment is still posted for the readable sessions and
  says "N session(s) could not be read"; when no file can be read at all, nothing is posted and the
  exit code is `3`. `--allow-empty` (and the `allow-empty` action input) turns an empty session folder
  into a notice, a one-line job summary and exit `0` instead of an error. A failed post now always
  exits `3`, printing the posting error and the threshold message together, so a red job never hides
  that the comment never went up. A fork pull request (read-only token: `Resource not accessible by
  integration`, or a `pull_request` event whose head repository is a fork) writes the full receipt to
  `GITHUB_STEP_SUMMARY`, prints a notice and exits `0` while still honouring `--fail-on`; every other
  403, and a 401, stay errors.

- **The Action covers every agent (`action.yml`):** the description and `sessions-dir` input name AI
  coding agent sessions (`.jsonl` and `.runledger.json`, Claude Code, Codex and Factory Droid) and
  the Droid `.settings.json` sidecar that carries usage, and two inputs are new: `allow-empty`
  (default `false`) and `python-version` (default `3.11`), which the setup step now uses.

- **A colon in a PowerShell target no longer hides a real file write (`runledger.ps_writes`):** the
  skip for non-filesystem provider targets now matches the providers' own names (`HKLM:`, `HKCU:`,
  `Cert:`, `Env:`, `Function:`, `Variable:`, `Alias:`, `WSMan:`), so a literal file such as
  `notes:2026.txt` or the drive-relative `C:notes.txt` is listed in `file_changes` again when
  written or deleted, and the literal path of a `[System.IO.File]` write is never read as a
  provider target.

- **The restore recipes in `docs/self-hosting.md` match what the backup script writes and the user
  the server runs as (`docs/self-hosting.md`, `docs/analysis.md`):** the Docker database restore
  reads `deploy/backups/<name>.db` (`/backup/backups/<name>.db` in the helper container), and both
  it and the bare-metal recipe hand the restored file to the service user (`runledger:runledger`,
  mode `0600`) with a writable `/data` (`0700`) before the server starts, because a backup is
  owner-only and the helpers run as root. The volume archive is now described as the data volume
  only: Caddy's certificates live in the separate `caddy-data` and `caddy-config` volumes, and no
  restore recipe removes a volume (`docker compose down -v` would delete volumes the archive does
  not hold). The whole-volume tar restore reads the archive the backup command writes
  (`runledger-data-YYYY-MM-DD.tgz`, not a compact date) and does every check and every write in one
  helper container running one `sh -c` with `set -e`: it checks the archive is readable, lists it
  with `tar tzf`, extracts its data-directory-relative members into a `/restore` directory of their
  own, checks that the database is there, and only then empties `/data` and copies the tree in
  (`cp -a /restore/. /data/`), giving the volume back to the service user with the database at mode
  `0600`. The service starts only when that container exited 0 (`&&`), so a missing, unreadable,
  corrupt or unusable archive leaves the volume exactly as it was and the server stopped.
  `docs/analysis.md` states which provider names are skipped,
  that a colon elsewhere is part of a file name, and that a `[System.IO.File]` call counts anywhere
  in a command.

### Security

- **A cross-site `GET /?key=` link cannot sign the browser in to another team (`runledger.server`):**
  the legacy sign-in now applies the same origin check as `POST /session`. A request a browser marks
  as coming from another origin (`Sec-Fetch-Site: cross-site` or `same-site`, or an `Origin` header
  that is not this server's own origin) is refused with `403 cross_site` before the key is checked:
  no cookie, no session row and a warning line on stderr that never contains the key. Plain links
  (a click, a pasted URL, curl or any client that sends no `Sec-Fetch-Site`) are unchanged, so
  `runledger serve` and `team create` links keep working.
- **A cross-site form cannot sign the browser in to another team (`runledger.server`):** `POST /session`
  now refuses a request that a browser marks as coming from another origin, before the body is read,
  with `403 cross_site`, no cookie and no session row, and a warning line on the server's stderr that
  never contains the key. `Sec-Fetch-Site: cross-site` and `same-site` are refused (another port,
  scheme or subdomain is a different origin, which is what a page on a second loopback port sends),
  and so is an `Origin` header that is not this server's own origin, `null` included, when compared at
  the scheme the server believes the client used (so a reverse proxy it already trusts for
  `X-Forwarded-Proto` matches). The two headers are checked independently: `Sec-Fetch-Site:
  same-origin` still has to agree with a concrete `Origin`, so a request whose headers contradict
  each other is refused; the fetch metadata only excuses an absent `Origin` and the opaque `null`
  one, because every response carries `Referrer-Policy: no-referrer` and Chromium then serializes
  the Origin of the dashboard's own form POST as `null` (measured with Chrome 155). Because
  the session cookie is `SameSite=Strict`, a cross-site form arrives without the cookie the visitor
  holds, so the confirmation page could not be shown and the cookie it set replaced the visitor's
  session; that request is now refused instead. Same-origin sign-ins (the dashboard form) and clients
  that send neither header (curl, `runledger` and other non-browser callers) are unchanged.
- **Writes are authenticated before their body is read, and the failed-key limiter has a real memory
  cap (`runledger.server`):** every endpoint that needs a role (`POST /api/runs`, `POST /api/approvals`,
  `POST /api/approvals/<id>/decision`, `PUT /api/team/settings`, `POST /api/keys`, `PUT /api/budgets`)
  now identifies the caller first, so an oversized unauthenticated body is answered `401` (`403` for a
  role that is too low) without reading or draining it, and the connection closes; a body over the cap
  sent by a valid member is still `413`. The in-memory failed-sign-in table evicts the least recently
  failed address once it holds `max_tracked` (default 10 000) addresses, so distinct source addresses
  can no longer grow it without bound. `runledger.server.auth` imports `Any`, so
  `typing.get_type_hints()` resolves every function in the module.
- **Dashboard sign-in posts the key, and `?key=` never overrides another team's session
  (`runledger.server`):** the sign-in page has a form that posts the key to `POST /session`, so the
  key stays out of the address bar, history and proxy logs, and the session cookie is now
  `SameSite=Strict`. The legacy `GET /?key=` sign-in still works for a first visit (it redirects to
  strip the key and logs one deprecation warning per process), but when the browser already holds a
  session of a **different** team the server answers `200` with a confirmation page and switches only
  after that form is submitted; a key of the same team signs in without confirmation. Each key keeps
  at most 20 sessions (the oldest of that key is dropped), and other keys are unaffected.
- **Receipt HTML is served with a `sandbox` CSP, and the team database and its backups are
  owner-only (`runledger.server`, `runledger.cli`):** `GET /runs/<id>` keeps its `default-src 'none'`
  policy (no scripts, no external resources, `nosniff`) and adds the `sandbox` directive, so a stored
  receipt cannot navigate or submit forms even if its HTML is hostile. The team database file is
  created with mode `0600`, and `runledger backup` sets the same mode on every copy; both are no-ops
  where the OS has no POSIX modes, so Windows never fails on them.

### Added

- **A re-pushed session supersedes the run it replaces, and its history is kept (`runledger.server`):**
  pushing the same `session_id` again with the same key (or an admin key) replaces the stored run and
  answers `200 {"status": "updated", "id": ..., "revision": n}` instead of `409 run_conflict`. The
  replaced version is kept in the new `run_revisions` table with its receipt JSON, HTML, risk score and
  cost, the newest 20 versions per run; `runledger push` reports the revision it replaced. An identical
  re-push is still an accepted retry (`200`, `"status": "duplicate"`, no new revision), a different
  member key still gets `409` (a run stored before the server recorded pushing keys has none after
  the migration, so only an admin key can replace it),
  and a receipt older than the stored version (an earlier `ended`, or fewer steps) is still `409`. The
  run keeps its URL, its `created_at` and its approvals; its risk, quality, AI review, insights,
  dashboard counts, budget spend and audit log are recomputed from the new version, and the supersede
  is recorded as the `run.supersede` audit event (revision, replaced_revision). The purge deletes a
  run's history with the run.
- **Runs store the client's `agent_id`, `usage_source` and `cost_partial`, and list by `sort_at`
  (`runledger.server`):** the run list and the receipt carry the agent's id (`claude-code`), how the
  client measured usage (`messages`, `settings` or `missing`) and whether its cost is a lower bound, so
  a dashboard can label partial costs; an existing database gains the columns, the version history
  table and a `(team_id, sort_at)` index in place, and its rows are backfilled from what they already
  hold (no data is rewritten or dropped). Runs are listed, filtered by window and purged by `sort_at`
  (the start time, or the push time when a receipt has none), so a run without a start time is no
  longer sorted or counted by a different column than the one the list uses.
- **Optional key attribution, and audit events on their own retention period (`runledger.server`):**
  the new team setting `attribution` (`client`, the default, or `key`) decides whether a pushed run is
  stored under the name the client sent or under the label of the key that pushed it. With `key` a
  member cannot choose the displayed user (admin keys still push under the client's name for someone
  else), the client's own value stays in the stored receipt, the run reports `pushed_by` in the API,
  and per-user budgets and the dashboard follow the key label. Every change is a `team.settings` audit
  event. The new `RUNLEDGER_AUDIT_RETENTION_DAYS` variable deletes audit events on their own schedule
  (default 365 days while runs are pruned, `0` keeps them for ever), so audit history is no longer
  deleted on the runs' period.
- **`runledger push --safe` masks what the team server stores (`runledger.client`, `runledger.cli`):**
  `--safe` (or `RUNLEDGER_PUSH_SAFE=1`, or `"push_safe": true` in `~/.runledger/config.json`; the flag
  wins over the environment, which wins over the config) applies the same safe mode as `runledger
  receipt` to the receipt before it is sent, so the stored JSON receipt, the HTML and the dashboard
  show relative paths and `[EMAIL]` instead of local folders and e-mail addresses. The default is
  unchanged: without any of the three, the server gets the full paths and the project folder it
  groups runs by. `--safe` wins over `RUNLEDGER_PUSH_SAFE=0`. A push to a remote HTTPS server that is
  not masked prints one line on stderr: `tip: --safe (or RUNLEDGER_PUSH_SAFE=1) masks local paths and
  e-mail addresses before they are sent`; a loopback server and plain HTTP stay silent. The developer
  name from `--user` is kept as given: it is the name the dashboard attributes the run to.
- **Packaging and releases describe and check the whole product (`pyproject.toml`, `.github/`,
  `tools/check_release.py`):** the package description and keywords now name every supported agent
  (Claude Code, Codex CLI, Factory Droid, Aider), and the wheel discovers its packages instead of
  listing them, so a new subpackage ships without a second edit. CI runs the test matrix on Python
  3.9-3.13 on Ubuntu, Windows and macOS, caches pip, times every job out, builds the sdist and wheel
  and runs `twine check` on them, and lints `deploy/docker-compose.yml`; it no longer runs twice for
  a branch with an open pull request. Pushing a `v*` tag now refuses to publish unless the tag,
  `pyproject.toml`, `runledger.__version__` and the CHANGELOG heading agree and the tagged commit is
  on `main`, and after PyPI succeeds it creates the GitHub Release from that CHANGELOG section. The
  publish action is pinned by commit and Dependabot watches the actions, the Docker base image and
  the build tools.

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

[Unreleased]: https://github.com/kodji7202-code/runledger/compare/v0.6.1...HEAD
[0.6.1]: https://github.com/kodji7202-code/runledger/compare/v0.6.0...v0.6.1
[0.6.0]: https://github.com/kodji7202-code/runledger/compare/v0.5.2...v0.6.0
[0.5.2]: https://github.com/kodji7202-code/runledger/compare/v0.5.1...v0.5.2
[0.5.1]: https://github.com/kodji7202-code/runledger/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/kodji7202-code/runledger/compare/v0.4.1...v0.5.0
[0.4.1]: https://github.com/kodji7202-code/runledger/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/kodji7202-code/runledger/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/kodji7202-code/runledger/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/kodji7202-code/runledger/releases/tag/v0.2.0
[0.1.0]: https://github.com/kodji7202-code/runledger/commit/b756b1ba48e37fa9aa4247857bf7bd24f7e5dc15
