# The real-time guard

The guard is a Claude Code `PreToolUse` hook. Before each tool call runs, Claude Code runs
`runledger guard` with the call as JSON on stdin. The guard applies the same rules as the receipt
(see the code list below), decides **allow**, **ask** or **deny**, optionally asks a human on the
team server, and appends the decision to a log in the project.

Contents:

- [Install and test](#install-and-test)
- [What the guard looks at](#what-the-guard-looks-at)
- [Default policy](#default-policy)
- [Policy files](#policy-files)
- [Approvals on the team server](#approvals-on-the-team-server)
- [Fail-open, fail-closed and monitor mode](#fail-open-fail-closed-and-monitor-mode)
- [The guard log](#the-guard-log)
- [Troubleshooting](#troubleshooting)

## Install and test

```bash
runledger guard install                  # this project (see project root below): writes <project root>/.claude/settings.json
runledger guard install --project PATH   # the project at PATH instead of the current folder
runledger guard install --global         # every project: writes ~/.claude/settings.json
runledger guard install --portable       # write bare "runledger ..." commands instead of "<python>" -m runledger ...
runledger guard uninstall                # remove it from this project (also --project PATH, --global)
runledger guard test 'EVENT_JSON'        # print the decision for one event; writes no log
```

`install` adds one entry to `hooks.PreToolUse`, with matcher `*`, command
`"<sys.executable>" -m runledger guard` (or bare `runledger guard` with `--portable`) and a
`timeout`. It also adds `"<sys.executable>" -m runledger checkpoint --hook` (or bare
`runledger checkpoint --hook` with `--portable`) to `SessionStart` and `UserPromptSubmit`
so each run and prompt gets a rollback point. Using the current Python interpreter path by default
means the hook works even when `runledger` is installed in a virtual environment that is not on the
`PATH` Claude Code starts with, and when the interpreter path contains spaces.
Without `--project`, the settings file is written at the project root, so installing from a
subfolder configures the whole project.
After writing `settings.json`, `install` runs the configured hook command once with `{}` on stdin
(timeout 15 seconds), from the project folder and with your environment exactly as it is, to verify
it exits `0`; it prints a warning if the command cannot be run or cannot start RunLedger there.
These checkpoint hooks require Git, but no identity
setup. They never block the agent and do not store prompt text in checkpoint metadata.
See [rollback.md](rollback.md) for setup, privacy, retention, and undo.

It keeps every other setting. Before it changes a file it copies the file to
`settings.json.bak`, replacing any earlier backup. If the file already has a guard or checkpoint entry, its
command and timeout are updated in place and no second entry is added. If nothing would change, the file is not
written. If the file is not valid JSON or is not an object, `install` stops with an error and
changes nothing. A hook that only mentions the words (for example `echo runledger guard` or
`other-check.sh && runledger guard`) is treated as your own hook and left alone; only the complete
commands `runledger guard` and `"<python>" -m runledger guard` count as RunLedger's.

`uninstall` removes every complete `runledger guard` hook and both lifecycle checkpoint hooks (both interpreter-form
and bare `--portable` entries) from the
chosen settings file and keeps everything
else, including other hooks in the same group, and hooks that merely mention the command. A group
left empty is removed, and so are an empty
event list and an empty `hooks` object. The file is copied to `settings.json.bak` first. If
there are no RunLedger hooks, the file is not written. Your policy files, checkpoints and `guard.log` stay where they
are.

Choose one scope. Each settings file you install into gets its own entry, so installing in both
`~/.claude/settings.json` and a project's `.claude/settings.json` would check every call twice.

When `runledger guard` is run interactively in a terminal (stdin is a TTY) with no subcommand, it
prints the `guard` help text and exits with code `2` instead of waiting on stdin.

**Project root resolution.** For every hook call, the guard resolves the project root in order:
1. `CLAUDE_PROJECT_DIR`, `FACTORY_PROJECT_DIR`, or `DROID_PROJECT_DIR` (when set to an existing absolute directory).
2. The nearest ancestor of the event's `cwd` (including the `cwd` itself) that contains `.runledger.json` or `.git`, so a policy-only project without Git works the same way.
3. The event's `cwd`.

The guard always reads `<project_root>/.runledger.json`, writes `<project_root>/.runledger/guard.log`,
and evaluates `write_outside`, `read_outside` and `shell_outside` against `<project_root>` (while
resolving relative paths in the step against the event's `cwd`), so running a tool from a subfolder
still enforces the root policy and keeps one log at the project root. Receipts read `<project_root>/.runledger/guard.log`
as well as any legacy subfolder `.runledger/guard.log` and de-duplicate identical entries.

**Hook timeout.** `install` writes the hook timeout as the approval wait plus 15 seconds: 135
seconds with the default wait of 120 seconds. The hook must outlive the approval wait, or the
answer arrives after Claude Code has stopped the hook. If you change `timeout_s`, run `install`
again; it updates the existing entry. A project's own `timeout_s` is used when you install for that
project. A `--global` install uses the wait from your user file.

`guard test` takes one event as JSON. On Windows, double the backslashes in paths:

```bash
runledger guard test '{"session_id":"s1","cwd":"D:\\my-app","tool_name":"Bash","tool_input":{"command":"rm -rf /"}}'
```

The output is an object with `decision` (the full decision, including `risks` and `warnings`) and
`hook_output` (what the hook would print). `test` uses the project file in the
event's `cwd` and your user file. If an approval server is configured and the decision is ask,
`test` sends a real approval request and waits for an answer, as the hook does.

The event format is the one Claude Code sends: `session_id`, `cwd`, `tool_name`, `tool_input`
and `tool_use_id`. The tool id links the decision to its step in the receipt.

## What the guard looks at

The guard scores each call with the rules in `runledger/risk.py`. Each finding has a **code**, a
**severity** (`high`, `medium` or `low`) and a reason. Policy lists match on codes and severities.

| Code | Severity | Matches |
| --- | --- | --- |
| `secret_in_content` | high | A Write, Edit, MultiEdit or NotebookEdit call that writes a hardcoded secret. Only the kind is reported: for example "Anthropic API key", "AWS access key", "private key", or a generic `api_key`, `secret`, `token` or `password` value of 12 or more characters. |
| `secret_file` | high | Touching a secrets file: `.env` (but not `.env.example`, `.sample`, `.template` or `.dist`), `.pem`, `.key`, `.p8`, `.p12`, `.pfx`, `.jks`, `.keystore`, `.kdbx`, `id_rsa` and related private keys (but not `*.pub` public keys), `.tfstate`, `.tfvars`, `.npmrc`, `.pypirc`, `.netrc`, `.pgpass`, `.htpasswd`, `.vault-token`, `.aws/credentials`, `.aws/config`, `credentials*`, `secrets.yaml|json|toml`, `.git-credentials`, `.git/config`, `service-account*.json`, `.docker/config.json`, `.kube/config`, `.config/gh/hosts.yml`, `.config/gcloud/credentials.db`, `.azure/accessTokens.json`. Covers Read, Write, Edit, Delete and shell commands. |
| `write_outside` | high | Write, Edit, MultiEdit, NotebookEdit or Delete of a path outside the working folder (including UNC shares, `\\?\` long-path prefixes, and local symlinks or junctions that resolve outside the project). Temp folders do not count. |
| `shell_outside` | low, medium or high | A shell command that uses a path outside the working folder. Reading or writing is low, deleting is high, and running a command with `workdir` outside the folder is medium. The patterns of `grep`, `rg`, `ag` and `git grep` and the scripts of `sed` and `awk` are not treated as paths. |
| `read_outside` | low | Read of a path outside the working folder. |
| `command` | high | `rm -rf`, a force push, `curl ... \| sh` and similar downloads run in a shell, destructive SQL (`DROP`, `TRUNCATE`), `npm`, `twine` or `cargo publish`, `kubectl` or `terraform` apply, delete or destroy, `sudo`, and in PowerShell `Remove-Item -Recurse -Force`, `iwr ... \| iex` and `Start-Process -Verb RunAs`. |
| `command` | medium | `git push` (any), `git reset --hard`, `git clean -f`, `chmod 777`, printing environment variables or secrets, `--no-verify`, `taskkill /IM`, `Set-ExecutionPolicy`, listing `env:`. |
| `command` | low | Installing packages (`npm`, `pnpm`, `yarn`, `pip`, `brew`), and network requests from the shell (`curl`, `wget`, `http`). |
| `test_deleted` | high or low | Deleting a test file (high). Deleting a fixture or data file under a test folder (low). |
| `test_skipped` | high | An edit that adds a skip, only, ignore or disabled marker to a test. |
| `test_weakened` | medium | An edit to a test file that removes at least two assertions, or removes at least 15 more lines than it adds. |
| `git_internals` | medium | A write or delete inside a `.git` folder. |
| `mcp` | low | Any call to an MCP tool. The reason names the tool. |

The guard reads each risk's severity and code, not the receipt's 0 to 100 score. The receipt score
is described in [quickstart.md](quickstart.md) and in the README.

## Default policy

Each policy list has a default, used when your user file does not set that list:

- **deny:** `secret_in_content`, `command:high`, and the exclusion `!reason:ran a command with sudo`.
- **ask:** `severity:high`.
- Everything else is **allowed** (medium and low findings, and calls with no findings).

So `rm -rf`, a force push and a hardcoded API key written into a file are denied. `sudo`, reading
a `.env` file, writing outside the project and deleting a test file ask. A `git push` (medium) is
allowed unless a policy file says otherwise.

## Policy files

There are two settings files, and they are not equal:

| File | Owner | What it can do |
| --- | --- | --- |
| `~/.runledger/config.json` (or `$RUNLEDGER_CONFIG`) | You | Everything: mode, lists, `ignore`, `severity_overrides`, `extra_secret_paths`, approval server, approval key variable, timeouts, fail-closed. The home folder is `HOME`, then `USERPROFILE`. |
| `<project>/.runledger.json` | The repository | Only tighten: add to the lists, raise severities, add safe `extra_secret_paths`, turn on fail-closed, and switch monitor to enforce. Anything that would loosen the guard or hide/lower receipt risks is ignored. |

Both files are JSON. Put the guard settings under the `guard` key. The risk settings `ignore`,
`severity_overrides` and `extra_secret_paths` sit at the top level of each file.

What each file can set:

| Setting | Your user file | The project file |
| --- | --- | --- |
| `guard.mode`: `enforce` or `monitor` | Sets either. | Can switch monitor to enforce. It cannot switch enforce to monitor. |
| `guard.fail_closed` | Sets it. | Turns it on. A project cannot turn it off. |
| `guard.deny`, `guard.ask` | Replaces the default list. | Adds entries to the effective list. Nothing is removed. |
| `!` exclusions | Cancel matches within the same list. | Cancel matches within the project's own list only. They cannot remove a user or default rule. |
| `guard.approval.timeout_s` | Sets it. | Sets it. A project value wins if present. |
| `guard.approval.on_timeout` | `deny` or `ask`. | `deny` only. `ask` would turn a timeout denial into a local prompt, so only your file can set it. |
| `guard.approval.server` | Sets it. | Ignored. |
| `guard.approval.api_key_env` | Sets it. | Ignored. |
| `ignore` | Hides matching findings from the guard and the receipt. | Ignored by both the guard and the receipt (the receipt adds a note). |
| `severity_overrides` | Sets any severity. | Can raise a severity, never lower it (in both the guard and the receipt). |
| `extra_secret_paths` | Adds safe regular expressions or `glob:...` patterns (up to 20, max 200 characters each). | Adds safe regular expressions or `glob:...` patterns. Unsafe backtracking patterns are ignored and logged. |

The server and key can come only from the environment or your user file. A repository cannot send
your key to a server of its choosing. See [approvals](#approvals-on-the-team-server).

### Entry syntax

Every list accepts these entries:

| Entry | Matches |
| --- | --- |
| `mcp` | A risk code (case-insensitive). |
| `severity:high` | Any finding at that severity: `high`, `medium` or `low`. |
| `reason:force push` | Findings whose reason contains the text (case-insensitive). |
| `command:high` | A code and a severity together. |
| `!` in front of any entry | An exclusion, applied only within the same list. |

Entries that do not parse, such as `command:critical`, are ignored. Within a decision, the most
restrictive outcome wins: any deny beats any ask, and any ask beats allow. Each list is checked on
its own. A project's exclusion that would remove a user or default match is ignored and logged as a
warning.

### Example: your user file

```json
{
  "server": "https://runledger.example.com",
  "api_key_env": "RUNLEDGER_API_KEY",
  "guard": {
    "mode": "enforce",
    "deny": ["secret_in_content", "command:high", "!reason:ran a command with sudo"],
    "ask": ["severity:high", "mcp"],
    "fail_closed": false,
    "approval": { "timeout_s": 120, "on_timeout": "deny" }
  }
}
```

`server` is read from this file only if `RUNLEDGER_SERVER` is not set. The environment variable
wins.

### Example: a project file that tightens the guard

```json
{
  "guard": {
    "deny": ["mcp"],
    "ask": ["severity:medium"],
    "fail_closed": true,
    "approval": { "timeout_s": 60 }
  },
  "severity_overrides": { "shell_outside": "high" },
  "extra_secret_paths": ["(^|/)config/prod\\.ya?ml$"]
}
```

With this file, MCP calls are denied, medium findings ask, failures deny, and the approval wait is
60 seconds. A repository cannot set `server`, `api_key_env` or `on_timeout: "ask"`, and any such
value is logged as a warning.

Warnings appear in the log, for example:

```
Ignored approval.server in .runledger.json: a project file cannot set the approval server or key.
Ignored mode monitor in .runledger.json: a project can turn enforcement on, not off.
Ignored exclusion "!command:high" in the project's deny list: an exclusion cannot remove a rule from the user or default deny list.
```

## Approvals on the team server

A call that is an **ask** can wait for a person. This needs three things: the decision is ask, the
mode is enforce, and an approval server is configured.

- **Server:** `RUNLEDGER_SERVER`, or `server` in your user file. The environment variable wins.
- **Key:** the variable named by `api_key_env` in your user file, which defaults to
  `RUNLEDGER_API_KEY`. The key needs the **member** or **admin** role on that team. A viewer key
  gets 403, which falls back to the local prompt.

The flow:

1. The guard sends `POST {server}/api/approvals` with the call's session id, tool, a short summary,
   the findings and the working folder. The server answers `201` with an id.
2. The guard polls `GET {server}/api/approvals/{id}` every 2 seconds.
3. A person decides the approval on the dashboard (`/approvals/{id}`), or through the API
   (`POST /api/approvals/{id}/decision`). See [api.md](api.md).
4. The guard answers Claude Code from the decision.

| Outcome | What the hook prints | Reason shown to Claude |
| --- | --- | --- |
| Approved | `allow` | "Approved in RunLedger: ..." |
| Denied | `deny` | "Denied in the RunLedger approval queue: ..." |
| Expired (the team's TTL passed) | `deny` | "Approval expired before a decision: ..." |
| No answer within `timeout_s` | `deny`, or a local `ask` if `on_timeout` is `ask` | "No approval within 120s: ..." |
| Server unreachable, bad key, or an HTTP error | `ask`, which is the local prompt | "... (approval unavailable: ...)" |

An approval is the only way the guard prints `allow`. A plain allow prints nothing, so Claude Code's
own permission rules still decide. An approved call skips Claude Code's prompt for that one call.

The summary sent to the server is cut at 200 characters and has known secrets masked (the same
masking as the log, described below). Each finding's reason is cut at 300 characters. The session
id and the folder are sent unchanged. If the team has a Slack or webhook URL set, the server also
notifies it about each new approval, and those messages include the same summary and findings.

## Fail-open, fail-closed and monitor mode

**Fail open (the default).** If the guard cannot evaluate a call, for example because the input is
not valid JSON or a bug occurs, it prints nothing. Claude Code's normal permissions then apply. If
the settings files cannot be read at all, the guard also fails open.

**Fail closed.** Set `"fail_closed": true` in either file. An evaluation error then becomes a deny
whose reason says the guard could not evaluate the call.

**Monitor mode.** Set `"mode": "monitor"` in your user file. Monitor mode never blocks or asks. It
prints nothing, logs what it would have done, and ignores `fail_closed`. Use it to see what the
rules would stop before you enforce them. Only your user file can turn monitor mode on. A project
file that says `enforce` switches a monitor setting in your file to enforcement.

## The guard log

Each project's decisions go to `<project>/.runledger/guard.log`, where `<project>` is the folder in
the event's `cwd`. Each line is a JSON object:

```json
{"ts": "2026-10-08T16:39:27+00:00", "session_id": "s1", "tool_use_id": "toolu-1", "tool": "Bash", "mode": "monitor", "decision": "allow", "policy_decision": "deny", "codes": ["command"], "approval": null, "summary": "rm -rf /", "reason": "Monitor mode, not enforced. Blocked by RunLedger guard: Recursive force delete (rm -rf)"}
```

- `decision` is what happened (`allow`, `ask`, `deny`, or `error`). `policy_decision` is what the
  rules said before monitor mode and approvals changed it.
- `approval` is `approved`, `denied`, `expired`, `timeout` or `unavailable` when an approval was
  asked for, otherwise `null`.
- A line with `"decision": "warning"` records a project setting the guard ignored.
- A line with `"decision": "error"` records an evaluation error.
- Every decision is logged, including plain allows, server approvals and monitor-mode
  decisions. Receipts omit plain allows; the audit log retains them.

### Guard decisions in receipts

HTML, Markdown and JSON receipts read the log from the run's recorded working folder, not the
folder where you invoke `receipt`. Only the run's `session_id` is included. The log is streamed;
missing or unreadable files, corrupt lines and oversized lines (over 64 KiB of text) are skipped.

The **Guard decisions** section shows the tool, summary, decision, outcome, reason, codes and
timestamp. It links to a step by `tool_use_id`. Older records without an id match the tool and
Guard summary in transcript order. Records with no matching step remain visible as **unmatched**.
A blocked call is a requested step, not evidence that the tool executed.

| Decision | Receipt outcome |
| --- | --- |
| Deny | **Blocked by Guard** |
| Ask, with a normal tool result (including an empty result or a tool execution error) | **Approved by user** |
| Ask, with Claude Code's user-rejection/permission-denial message, or no result | **Rejected by user** |
| Approval status `approved` or `denied` | **Approved by approval server** or **Denied by approval server**, regardless of the local result |
| Monitor mode | **Would have been blocked (monitor mode)** or **Would have been asked (monitor mode)** |

An expired approval or a timeout that denies the call is **Blocked by Guard**. If the server is
unavailable or times out and falls back to a local ask, the tool result determines the outcome.
An ask with no result is reported as rejected even if the session is still in progress.

The summary includes counts such as `Guard: 1 blocked, 2 asked (2 approved)`. Server-approved and
server-denied requests count as asked; monitor counts are separate from enforced decisions.
JSON adds a `guard` list of `{step, tool, summary, decision, outcome, reason, codes, ts}` objects,
where `step` is the one-based step index or `null`, plus counts under `totals.guard`
(`blocked`, `asked`, `approved`, `rejected`, `monitor_blocked`, `monitor_asked`).
`decision` retains the hook's final decision, so a monitor record can say `allow`.
The section, JSON key and counts are omitted when no relevant records exist. Plain allows,
warnings and evaluation errors are not shown in this section. Summaries and reasons are masked
again before inclusion in a receipt.

The log records the tool name, the **summary**, the codes and the reason. For shell tools the
summary is the command. For file tools it is the path. Write and Edit bodies, file contents and MCP
arguments are never logged. Each string is cut at 200 characters.

**Redaction is pattern-based.** Before writing, the guard masks secrets in the summary and the
reason. Receipts use the same masking. It recognises:

- token formats: `sk-ant-…`, `sk-…`, `AKIA…`, `ghp_…` and other GitHub tokens, GitLab `glpat-…`,
  Slack `xox…`, Stripe `sk_live_…` and `rk_live_…`, Google `AIza…`, npm `npm_…`, Hugging Face `hf_…`,
  RunLedger `rl_…` keys, JWTs, `Bearer …` and `Authorization: Basic …` values, and private-key headers;
- quoted assignments such as `api_key="…"`, `secret='…'`, `token: "…"` and `password="…"` with a value
  of 8 or more characters;
- unquoted assignments to a name containing `api_key`, `access_key`, `private_key`, `secret`,
  `token`, `password`, `passwd` or `credential`, such as `export DB_PASSWORD=…`, `TOKEN=… npm publish` or `?access_token=…` in a URL,
  and flags such as `--token …` and `--password …`;
- the password in a URL such as `postgres://user:…@host`.

An unquoted value is masked when it has a digit or is at least 12 characters long, so `token=refresh`
stays as it is. Variable references (`$TOKEN`, `%TOKEN%`), placeholders (`<your key>`) and names in
capitals such as `GITHUB_TOKEN` are not masked. A secret in another format, or a short one, can still
get through. Treat `guard.log` as sensitive and keep it out of version control. The
RunLedger repository's `.gitignore` lists `.runledger/`, but your project's `.gitignore` does not
include it unless you add it.

## Troubleshooting

- **No decisions appear at all.** Run `runledger guard test` with an event for a risky command. If
  it prints a deny, the guard works and the problem is the hook. Check that
  `.claude/settings.json` has a `PreToolUse` entry with `runledger guard`, that `runledger` is on
  Claude Code's PATH.
- **Approvals never reach the dashboard.** Check `RUNLEDGER_SERVER` and the key variable, and that
  the key has the member role. Look for `approval: "unavailable"` in `guard.log`. The reason text
  says why, for example `HTTP 401` for a wrong key.
- **Calls are denied with "could not evaluate".** Fail-closed is on. The reason after the colon names
  the error. Check the event JSON and the project file.
- **Approval waits end before the answer.** Run `runledger guard install` again after you change
  `timeout_s`, so the hook timeout follows it.
- **A rule you expected does not match.** Check the entry syntax. An entry that does not parse is
  ignored without a warning.
