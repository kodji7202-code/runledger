# Doctor: check the installation

`runledger doctor` inspects one project and reports what it found, check by check. It answers the
questions a stuck user has: is the hook installed for this agent, is the stored command still
runnable, is my config valid, can the guard reach the approval server, and does anything else
about this installation need attention.

```bash
runledger doctor
runledger doctor --project PATH
runledger doctor --agent codex
runledger doctor --offline
runledger doctor --no-selftest
runledger doctor --json
runledger doctor --verbose
runledger guard status
```

| Flag | Effect |
| --- | --- |
| `--project PATH` | Report on that folder instead of the current one. |
| `--agent A` | Limit the agent-specific checks to `claude-code`, `droid` or `codex`. Repeat it for several. Agent-independent checks stay. |
| `--offline` | Do not contact the approval server. The approval check then only reports the configuration. |
| `--no-selftest` | Do not run the stored hook command; the self-test becomes a `skip` line. |
| `--json` | Print one JSON object instead of the text report. |
| `--verbose` | Add the config paths, hook file paths and other environment detail behind each line. |

`doctor` is **read-only**: it reads files and runs the hook self-test in a temporary folder, and it
never writes into your project, your home folder or Git. No `.bak` file, no `.runledger` folder, no
`guard.log` row, no config file, no checkpoint ref, no session cache. Its only network request is
the optional `GET <server>/health` of the
approval check, and that request is skipped by `--offline`. Secrets are never printed: the
approval check reports the key variable as `set (env RUNLEDGER_API_KEY)` or `not set`, never its
value, in the text report, in `--json`, in `--verbose` and on stderr.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Every check is `ok`, `info` or `skip`. |
| `1` | At least one `warn` and no `fail`. |
| `2` | At least one `fail`. |

A folder that is not a Git repository, or an agent that has no hooks installed, is not a failure:
"nothing installed" is `info`. Invalid JSON in a hook file, a hook command whose interpreter does
not exist, an invalid config file or an unreadable `prices.json` are failures.

## The text report

One line per check, with the status, the check title, the agent and scope when the check belongs
to one, and the detail. The first line is a header with the RunLedger version, the Python version,
the platform and the project folder. Warnings and failures add a `->` line with what to do:

```text
[ok] install: runledger 0.7.0 | python 3.12.10 | win32 | D:\my-app | runledger on PATH (C:\Python312\Scripts\runledger.exe)
[ok] guard hooks claude-code project: Claude Code project: guard hook (PreToolUse) and 1 + 1 checkpoint hooks (SessionStart, UserPromptSubmit) in D:\my-app\.claude\settings.json
[info] guard hooks codex project: Codex project: not installed (D:\my-app\.codex\hooks.json)
[info] guard notes codex: Codex: Codex runs only hooks you trust: open `codex` and review the RunLedger entries in /hooks. ...
[warn] log: D:\my-app\.runledger\guard.log: 412 bytes; 3 row(s); last decision 2026-10-19T15:55:02+00:00; 0 error row(s) in the last 7 days; .runledger/ is not git-ignored: guard.log holds command summaries
        -> Add `.runledger/` to .gitignore: the log holds command summaries you do not want to commit.
Summary: 6 ok, 1 warnings, 0 failures
```

The summary line counts only `[ok]`, `[warn]` and `[fail]` lines, so its numbers always match the
report above it.

## The JSON report

`--json` writes exactly one object on stdout:

```json
{
  "version": 1,
  "runledger": "0.7.0",
  "project": "D:\\my-app",
  "summary": {"ok": 8, "warn": 1, "fail": 0, "info": 4},
  "checks": [
    {
      "id": "log",
      "status": "warn",
      "title": "guard log",
      "detail": "...",
      "fix": "...",
      "agent": null,
      "scope": null
    }
  ]
}
```

- `version` is the report format (`1`), `runledger` the installed version, `project` the folder
  the report is about.
- `summary` counts the checks by status: `ok`, `warn`, `fail` and `info`.
- `checks` is one entry per check: `id` (stable, so CI can assert on it), `status`, `title`,
  `detail`, `fix`, `agent` and `scope`. Both `agent` and `scope` are `null` for a check that is
  not about one agent or one scope, and `fix` is `""` when there is nothing to do. `status` is one
  of `ok`, `warn`, `fail`, `info` and `skip`.
- A `skip` check is not counted in `summary`, the same way the text report's summary line leaves
  it out: `doctor --no-selftest` shows one `guard.selftest` entry with status `skip`.

The check ids are `install`, `guard.hooks`, `guard.note`, `guard.selftest`, `config`, `approval`,
`log`, `rollback`, `pricing`, `sessions` and, in `guard status`, `guard.mode`.

## The checks

### `install`

The RunLedger version (the same string as `runledger --version`), the Python version (3.9 or newer
is required, an older interpreter is a failure) and how the CLI is reached: the `runledger.exe`
that `PATH` resolves to (`ok`), or "not on PATH, running as `python -m runledger`" (`info`). The
second one is common in a virtual environment and is not a problem.

### `guard.hooks` (one line per agent and scope)

Claude Code, Droid and Codex, project and global: six lines. For every combination doctor reads
that agent's hook file and reports:

| State | Status | Detail |
| --- | --- | --- |
| No file, or no RunLedger hook in it | `info` | `not installed` and the file path |
| Guard hook, `SessionStart` and `UserPromptSubmit` checkpoint hooks present | `ok` | the file and the entries found |
| A hook is missing (for example a hand-deleted `SessionStart` entry) | `warn` | what is missing and how to rewrite it |
| A `PreToolUse` command in a file that still holds RunLedger hooks is not a RunLedger guard command | `warn` | that command, which doctor never runs, and how to rewrite the entry |
| The same file runs the guard twice | `warn` | the duplicate entries |
| Hook `timeout` smaller than the approval wait needs | `warn` | the agent would run the tool before the approval answers; re-run `guard install`. The wait is the project's `guard.approval.timeout_s` when it sets one, for a global hook too: the hook reads the project's file when it runs |
| A `--portable` command (`runledger guard`) and `runledger` is not on `PATH` | `warn` | the hook depends on `PATH`; reinstall without `--portable` or add it to `PATH` |
| The hook command's interpreter path does not exist | `fail` | the interpreter that is missing; re-run `guard install` |
| The file is not valid JSON | `fail` | the file and the line and column of the parse error |
| Codex, on Windows: the entry's `commandWindows` is not a RunLedger command, or its interpreter does not exist | `fail` | that copy is the command Codex runs on Windows, not `command` |

### `guard.note` (Codex trust, Droid restart)

An `info` note where the hook is installed and the agent needs something from you:

- **Codex** runs only hooks you trust. Open `codex`, review the RunLedger entries in `/hooks` and
  trust them. RunLedger cannot read that state from outside, so doctor never reports the Codex
  guard as active or trusted; it reports the installation and says the trust state is unverified.
- **Droid** snapshots its hooks at startup. Restart droid, or review the entries in `/hooks` in a
  running session, before the guard takes effect.

Notes never raise the exit code.

### `guard.selftest`

For each agent whose hook is installed, doctor runs the **stored command string** through the
shell, exactly as the agent would, with a synthetic `PreToolUse` payload in that agent's real
shape: `rm -rf /` must produce deny JSON, `ls` must produce empty output (nothing to report means
allow). The result line is `ok` with the elapsed time. It is a `fail` when the command does not
start, exits non-zero, prints something else, or fails to deny the destructive call, and the
detail then carries what was observed: exit code, stdout and stderr (truncated).

What is run belongs to RunLedger, and ownership is decided on the command that would run, never on
another entry in the same file. The installed guard entry is the normal case. A file that still
holds RunLedger hooks - a checkpoint entry, say - but whose `PreToolUse` command is no longer a
RunLedger guard command is reported and **not** run: the `guard.hooks` entry warns and names the
command, and when no scope holds an owned command at all the self-test itself is the `fail` (exit
2) that says why it declined, instead of executing a command anyone could have written. A foreign
project hook (someone else's `echo` preflight) in a file RunLedger never touched is neither run
nor reported as a RunLedger failure, and it does not hide a RunLedger entry installed in another
scope: doctor keeps looking and self-tests that one. For Codex, the `commandWindows` copy is the
command Codex runs on Windows, so that is the one doctor checks there - and runs there only when
RunLedger owns it; on macOS and Linux Codex runs `command`, and so does doctor.

The run is isolated: a temporary working directory, temporary `HOME`/`USERPROFILE`, temporary
agent config folders, no `RUNLEDGER_SERVER` and no API key variables, so a `monitor` mode or an
empty deny list in your own config cannot make it fail, no `.runledger` folder or log row appears
in your project, and no approval server is contacted. `--no-selftest` skips it (one `skip` line)
and starts no subprocess. A full run with three agents installed takes a few seconds.

### `config`

Two lines: your user file (`$RUNLEDGER_CONFIG`, else `~/.runledger/config.json`) and the project's
`.runledger.json`.

- Valid and tightening only: `ok`.
- No file: `info`.
- Not valid JSON: `fail`, with the file, the parse error and its line and column. The guard and
  the receipts ignore that file, so this is worth fixing before you trust your own rules.
- Empty, or only whitespace: `fail` (the file holds no JSON object). A file that is not UTF-8
  JSON is a `fail` too and says so: a config written as UTF-16 - an ordinary PowerShell `>`
  redirection - is read as UTF-8 by the guard and the receipts, which then ignore it.
- Unknown top-level keys: `info`, listing them.
- Invalid rule entries or regexes (dropped as unsafe or mistyped): `warn`, with a count.
- Project settings the guard ignores (`guard.mode: "monitor"`, a project-only approval server,
  `mcp_verbs.read`, a `severity_overrides` that lowers a severity, `guard.codex_ask: "allow"`,
  …): `warn`, naming each one. A project file may only tighten the guard, never loosen it, so
  doctor shows what it tried to loosen and what actually applies.

### `approval`

The approval server and key variable come from the environment or your user file, never from a
project file.

| State | Status |
| --- | --- |
| No server configured | `info` (the guard decides locally) |
| Server reachable (`GET /health` answers 2xx) | `ok` |
| Server configured but unreachable | `warn`: the guard then follows `on_timeout` for an ask |
| The server setting is not a usable URL (`approver.example`, no scheme) | `warn`: the same `on_timeout` line, naming what urllib rejected; the rest of the report is unaffected |
| `http://` on a non-loopback host | `warn`: cleartext would expose the approval key; use https |
| `--offline` | `info` (or the cleartext `warn`); no request is made |

The health request is unauthenticated: no approval key is ever sent, and the check only reports
whether the key variable is set. The timeout is 3 seconds, so an unreachable server delays the
report by that much at most.

### `log`

`<project>/.runledger/guard.log`: its size, how many rows it has, the time of the last decision,
and how many `decision: "error"` rows are from the last 7 days. Recent error rows mean the guard
failed open on those calls, so they are a `warn`; older errors do not warn. When `.runledger/` is
not covered by `.gitignore`, the check warns that `guard.log` holds command summaries you may not
want to commit. A log that is in the Git index warns as well, even when an ignore rule covers it:
a file committed before the rule was added keeps recording every change and stays publishable, so
the fix is `git rm --cached .runledger/guard.log` (which leaves it on disk). No log yet is `info`.
The check counts rows and never prints their contents, so no command text from the log appears in
the report.

### `rollback`

The Git version, how many checkpoints exist (`refs/runledger/checkpoints`, the same list as
`runledger checkpoints`), and the size of the newest checkpoint tree in bytes, files and largest
file. More than 100 checkpoints warn, with the pruning command:

```bash
runledger checkpoints prune --keep 50
```

Not a Git work tree, or no Git on `PATH`, is `info`/`skip`, not an error. The check reads refs; it
never creates, moves or deletes one.

### `pricing`

The bundled `runledger/prices.json`. Its `checked` date is compared with the 120-day threshold
used by receipts: older than 120 days is a `warn` showing the date and the age in days, a missing
or unreadable `checked` value is `info`, and a file that is missing or not valid JSON is a `fail`
(every cost estimate would be wrong or missing). The fix text names how to override prices:
`~/.runledger/prices.json` or `RUNLEDGER_PRICES`.

### `sessions`

One `info` line per agent (`claude-code`, `codex`, `droid`, `aider`, `native`): how many sessions
in this project RunLedger can discover, and the home directory it read them from
(`CLAUDE_CONFIG_DIR`, `CODEX_HOME`, the Factory directory, or the project for Aider and the open
format). The counts match `runledger list --agent X --limit 9999 --project PATH` for the same
folder. Discovery does not write the session cache while doctor runs.

## `runledger guard status`

The compact guard-only view, for a script or a quick look after `guard install`:

```bash
runledger guard status
runledger guard status --project PATH --json
```

It runs only the guard checks: `guard.hooks` for every agent and scope, `guard.note`, `config`,
`approval`, `log` and a `guard.mode` line with the effective mode (`enforce` or `monitor`) and
whether `fail_closed` is on. It does not run the self-test and does not show `install`, `pricing`,
`rollback` or `sessions`. The exit codes are the same 0/1/2 rule, and a project file that tries to
set `mode: "monitor"` shows up as an ignored-key warning while the mode line still says `enforce`.

```text
[ok] guard hooks claude-code project: Claude Code project: guard hook (PreToolUse) and 1 + 1 checkpoint hooks (SessionStart, UserPromptSubmit) in D:\my-app\.claude\settings.json
[ok] guard hooks claude-code global: Claude Code global: not installed (C:\Users\me\.claude\settings.json)
[info] guard mode: mode enforce; fail_closed off
Summary: 4 ok, 0 warnings, 0 failures
```

The lines shown are a sample of the real output: `guard status` prints one line per agent and
scope, then the notes, the config, approval and log lines, the mode line and the summary.

## Troubleshooting

- **The guard never runs.** `runledger doctor --project PATH` and read the `guard.hooks` line for
  that agent: `not installed` means the file is not there, and a `fail` line names the file that
  does not parse. Then check the self-test line: a failing self-test means the command is stored
  but does not run.
- **The self-test passes but the agent ignores the hook.** Codex needs the entries trusted in
  `/hooks`; Droid needs a restart. Doctor prints both as notes instead of guessing.
- **A warning about `PATH`.** The `--portable` install wrote a bare `runledger guard` command. Run
  `runledger guard install` (without `--portable`) in a folder the agent starts in, so the stored
  command carries the interpreter path.
- **An approval never answers.** The `approval` line says whether the server is reachable; if it
  is not, the guard follows `on_timeout`. Make sure the wait in your config matches the hook
  `timeout` doctor reports: re-run `guard install` after changing `timeout_s`.
- **"ignored key" warnings in `config`.** The project file tried to loosen the guard. Move that
  setting to your user file, or remove it.

See [guard.md](guard.md) for the guard itself, and [rollback.md](rollback.md) for checkpoints.
