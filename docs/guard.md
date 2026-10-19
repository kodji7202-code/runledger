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
changes nothing. Only the complete commands the installer writes count as RunLedger's, and they
count only when the whole entry is that one command: `runledger guard` (optionally with a path or
`.exe`) and `"<interpreter>" -m runledger guard`. `<interpreter>` is either the interpreter that
runs `install` (it writes the literal `sys.executable`, so a renamed copy such as
`agent-python.exe` is recognised too) or an interpreter name such as `python3.13t.exe`, `pythonw.exe`
or `pypy3`, so an entry written by this installer is always recognised by a later `install`,
`uninstall` or self-test. Any other entry is left alone: `echo runledger guard`,
`other-check.sh && runledger guard`, a pipeline, a redirection, a sub-shell, a second line, or a
command with an operator attached to it without spaces
(`true;/usr/bin/python3.13t -m runledger guard`) keeps its own entry, and `uninstall` never removes
it.

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

The guard scores each call with the rules in `runledger/risk.py` (`runledger/mcp_risk.py` grades MCP
tools). Each finding has a **code**, a **severity** (`high`, `medium` or `low`) and a reason. Policy
lists match on codes and severities.

| Code | Severity | Matches |
| --- | --- | --- |
| `secret_in_content` | high | A Write, Edit, MultiEdit or NotebookEdit call that writes a hardcoded secret. Only the kind is reported: for example "Anthropic API key", "AWS access key", "private key", or a generic `api_key`, `secret`, `token` or `password` value of 12 or more characters. |
| `secret_file` | high | Touching a secrets file: `.env` (but not `.env.example`, `.sample`, `.template` or `.dist`), `.pem`, `.key`, `.p8`, `.p12`, `.pfx`, `.jks`, `.keystore`, `.kdbx`, `id_rsa` and related private keys (but not `*.pub` public keys), `.tfstate`, `.tfvars`, `.npmrc`, `.pypirc`, `.netrc`, `.pgpass`, `.htpasswd`, `.vault-token`, `.aws/credentials`, `.aws/config`, `credentials*`, `secrets.yaml|json|toml`, `.git-credentials`, `.git/config`, `service-account*.json`, `.docker/config.json`, `.kube/config`, `.config/gh/hosts.yml`, `.config/gcloud/credentials.db`, `.azure/accessTokens.json`. Covers Read, Write, Edit, Delete and shell commands. |
| `write_outside` | high | Write, Edit, MultiEdit, NotebookEdit or Delete of a path outside the working folder (including UNC shares, `\\?\` long-path prefixes, and local symlinks or junctions that resolve outside the project). Temp folders do not count. |
| `shell_outside` | low, medium or high | A shell command that uses a path outside the working folder. Reading or writing is low, deleting is high, and running a command with `workdir` outside the folder is medium. The patterns of `grep`, `rg`, `ag` and `git grep` and the scripts of `sed` and `awk` are not treated as paths. |
| `read_outside` | low | Read of a path outside the working folder. |
| `command` | high | `rm -rf`, a force push, `curl ... \| sh` and similar downloads run in a shell, destructive SQL (`DROP`, `TRUNCATE`), `npm`, `twine` or `cargo publish`, `kubectl` or `terraform` apply, delete or destroy, `sudo`, `git push --delete`/`git push origin :branch`, `helm uninstall`, `gh repo delete`, `aws s3 rm --recursive`, `aws s3 rb --force`, `docker volume prune` (and `docker system prune --volumes`), `dd of=/dev/...`, `rd /s /q`, `reg delete` of a machine-wide key, a `DELETE FROM` without a `WHERE` in a shell command, and in PowerShell `Remove-Item -Recurse -Force`, `iwr ... \| iex` and `Start-Process -Verb RunAs`. |
| `command` | medium | `git push` (any), `git reset --hard`, `git clean -f`, `git branch -D` (or `--delete --force`), `git checkout .` and `git restore .`, `git stash drop`/`clear`, `find ... -delete` (or `-exec rm`), `chmod 777`, `docker system\|builder\|image\|container\|network prune`, `gh <resource> delete`, `aws s3 rm` of one object, `rsync --delete`, `reg delete` of a user key, `dd of=<file>`, printing environment variables or secrets, `--no-verify`, `taskkill /IM`, `Set-ExecutionPolicy`, listing `env:`, and in PowerShell `Remove-Item -Recurse` without `-Force`. |
| `command` | low | Installing packages (`npm`, `pnpm`, `yarn`, `pip`, `brew`), and network requests from the shell (`curl`, `wget`, `http`). |
| `test_deleted` | high or low | Deleting a test file (high). Deleting a fixture or data file under a test folder (low). |
| `test_skipped` | high | An edit that adds a skip, only, ignore or disabled marker to a test. |
| `test_weakened` | medium | An edit to a test file that removes at least two assertions, or removes at least 15 more lines than it adds. |
| `git_internals` | medium | A write or delete inside a `.git` folder. |
| `mcp` | high | A destructive MCP tool (`delete`, `drop`, `destroy`, `reset`, ...), or an execute-class tool (`supabase__execute_query`) whose SQL drops or truncates, deletes or updates without a `WHERE`, or drops a column. |
| `mcp` | medium | A state-changing MCP tool (`create`, `update`, `send`, `deploy`, ...), any other data-changing SQL statement (`INSERT`, `UPDATE ... WHERE`, `CREATE`, `ALTER`, `GRANT`), and an execute-class tool whose SQL the guard cannot read. |
| `mcp` | low | A read-only MCP tool (`get`, `list`, `search`, ...), an unknown tool name, or SQL that only reads. The reason names the tool. |
| `rule_deny` | high | A command or tool matched one of your own `command_rules.deny` or `tool_rules.deny` entries. The reason names the entry. Always enforced: adding `rule_deny` (or `!rule_deny`) to a policy list changes nothing. |
| `rule_ask` | medium | A command or tool matched one of your own `command_rules.ask` or `tool_rules.ask` entries. The reason names the entry (or the `reason` you gave it), and it always asks. |

A `rule_deny` finding is high and a `rule_ask` finding is medium, so `severity:high` and the other
list entries see them like any other finding. What no entry can do is switch them off: an
exclusion in your own or a project's list leaves a rule hit alone, and a project cannot remove a
rule you wrote. Delete the entry from the file that holds it to soften it.

MCP tools are named `mcp__<server>__<tool>` by Claude Code and Codex, and as the bare connector
name `supabase__delete_project` by Factory Droid. The guard grades them by the verb in the tool
name: the first word decides the read rule, so `get_delete_policy` and `list_deleted_items` stay
low; otherwise any word meaning destructive is high, any word meaning state-changing is medium,
and any other name stays low. Words are matched exactly, so `deleted_items` and `undelete_items`
are not destructive; camelCase, `-` and `.` split words, so `deleteBranch` and `delete-branch`
are. An execute-class call (the name carries `sql`, `query`, `statement` or `execute`, or a
`query`/`sql`/`statement` argument opens like a statement) is graded by its SQL instead: dropping
or truncating, a `DELETE` or `UPDATE` without a `WHERE`, and `ALTER TABLE ... DROP` are high; any
other statement that changes data is medium; read-only SQL keeps the low wording, and a call with
no SQL the guard can read is medium. The SQL text is never written to the reason, the log, an
approval request or a receipt.

Because a destructive MCP call is high, it **asks** under the default policy below. Lower
`mcp` in your own file (`severity_overrides`) or ignore it (`ignore`) to soften that, or add
`mcp:high` to your `deny` list to block destructive MCP calls outright.

The command rules added in 0.7.0 read the same blanked view, so `echo "git branch -D x"` is not
the command being run, and a `DELETE FROM` inside a heredoc body is not scanned (the same
limitation quoted text always had). The rule families, by severity:

- **medium:** force-deleting a git branch (`git branch -D`, `git branch -fD`,
  `git branch --delete --force`), discarding the working tree (`git checkout .`,
  `git restore .`), dropping stash entries (`git stash drop`, `git stash clear`),
  `find ... -delete` and `find ... -exec rm|unlink|shred`, `dd of=<file>`,
  `docker system|builder|image|container|network prune`, `aws s3 rm` of one object,
  deleting a `gh` resource (`gh release|issue|gist|label|secret|variable|cache|run|workflow
  delete`), `rsync --delete` (and `--del`, `--remove-source-files`), `reg delete` of a user
  key, printing environment variables or secrets, and in PowerShell `Remove-Item -Recurse`
  without `-Force`.
- **high:** deleting a remote branch or tag (`git push --delete`, `git push origin -d`,
  `git push origin :branch`), `dd of=/dev/...`, `docker volume prune` and
  `docker system prune --volumes`, `aws s3 rm --recursive` and `aws s3 rb --force`,
  `gh repo delete`, `helm uninstall` (or `helm delete`), a `DELETE FROM` without a `WHERE`
  in a shell command, `reg delete` of a machine-wide (`HKLM`) key, `rd /s /q` and
  `rmdir /s /q`, and in PowerShell `Remove-Item -Recurse -Force`.

`find ... -delete` also covers `find /tmp/x -type f -delete`: `find` deletes below every
starting path it was given. Those starting paths are then checked like any other delete argument,
so a search root outside the working folder (and not a temp folder) is reported as well.

The guard reads each risk's severity and code, not the receipt's 0 to 100 score. The receipt score
is described in [quickstart.md](quickstart.md) and in the README.

## Default policy

Each policy list has a default, used when your user file does not set that list:

- **deny:** `secret_in_content`, `command:high`, and the exclusion `!reason:ran a command with sudo`.
- **ask:** `severity:high`.
- Everything else is **allowed** (medium and low findings, and calls with no findings).

So `rm -rf`, a force push and a hardcoded API key written into a file are denied. `sudo`, reading
a `.env` file, writing outside the project and deleting a test file ask. A `git push` (medium) is
allowed unless a policy file says otherwise. A destructive MCP call
(`mcp__supabase__delete_project`) and an execute-class call whose SQL drops or rewrites rows ask
for the same reason: they are high findings. Whatever these two lists say, a finding one of your
own rules produced is always decided: a `rule_deny` hit is denied and a `rule_ask` hit asks.

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
| `mcp_verbs.destructive`, `mcp_verbs.write`, `mcp_verbs.read` | Adds words to the three MCP verb vocabularies. | Adds `destructive` and `write` words. A `read` list is ignored and logged: a read word would lower a destructive tool. |
| `tool_rules.deny`, `tool_rules.ask` | Adds `fnmatch` globs matched against the whole tool name (case-sensitive). | Adds globs. Nothing is removed. |
| `command_rules.deny`, `command_rules.ask` | Adds regular expressions matched against shell commands. | Adds expressions. Nothing is removed. |

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
| `rule_deny`, `rule_ask` | The codes of the findings your own rules produce. They behave like any other code, but the guard always keeps both on its effective lists, and no exclusion can drop a rule hit. |
| `!` in front of any entry | An exclusion, applied only within the same list. |

Entries that do not parse, such as `command:critical`, are ignored. Within a decision, the most
restrictive outcome wins: any deny beats any ask, and any ask beats allow. Each list is checked on
its own. A project's exclusion that would remove a user or default match is ignored and logged as a
warning.

### Rules you write yourself

Besides the built-in rules you can write your own, under three top-level keys. Both files may
carry them, and both only add: your rules and a project's rules are all applied, and nothing
removes a built-in rule.

`command_rules` matches the command of a `Bash` or `PowerShell` call. An entry is either a regular
expression, or an object with `pattern`, an optional `reason` (what the decision shows instead of
the pattern) and `raw`. The expression is searched case-insensitively:

- by default it reads the command the way the built-in rules do: comments, the text arguments of
  `echo`, `printf`, `Write-Output`, `git commit -m` and the like, and heredoc/here-string bodies
  are blanked first, so `git commit -m "myfancytool wipe"` is not the command being run;
- with `"raw": true` it reads the whole command, so a pattern can also match text the command only
  prints. Use it only for words that cannot appear in an unrelated message.

A pattern that is not a safe regular expression (unbalanced brackets, or one that can backtrack
catastrophically) is dropped, and the warning names it and the file it came from. `deny` entries
deny, `ask` entries ask, and a command that matches both is denied.

`tool_rules` matches the tool name with `fnmatch` globs (`*` and `?`), case-sensitively, against
the whole name. Rules apply to any tool, not only MCP ones: `Edit` can be denied, and
`mcp__vercel__*` can ask for every Vercel tool. A tool on both lists is denied.

`mcp_verbs` adds words to the three MCP vocabularies the classifier uses (see above), so a server
whose destructive tool is called something else is graded as you expect. A project file may extend
`destructive` and `write` only.

```json
{
  "command_rules": {
    "deny": ["\\bmyfancytool\\s+wipe\\b"],
    "ask": [{ "pattern": "\\bkubectl\\s+exec\\b", "reason": "shell into prod" },
            { "pattern": "secretword", "raw": true, "reason": "names a secret" }]
  },
  "tool_rules": { "deny": ["mcp__supabase__execute_query"], "ask": ["mcp__vercel__*"] },
  "mcp_verbs": { "destructive": ["nuke"], "write": ["sync"], "read": ["peek"] }
}
```

With this file `myfancytool wipe all` is denied (code `rule_deny`), `kubectl exec -it pod -- sh`
asks (code `rule_ask`, reason "shell into prod"), `echo secretword` asks through the raw entry,
`mcp__supabase__execute_query` is denied whatever SQL it carries, and `mcp__foo__nuke_cluster` is
high while `mcp__foo__peek_queue` stays low.

**Receipts see the project's rules only.** A receipt re-runs the rules over a transcript with the
`.runledger.json` of the folder the session ran in, so that a receipt is reproducible from the
repository it belongs to. A rule that lives only in your user file is enforced by the live guard
and does not appear in a receipt built from an older session. Put a rule you want in receipts in
the project file instead.

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
  },
  "mcp_verbs": { "destructive": ["obliterate"] },
  "tool_rules": { "deny": ["mcp__supabase__execute_query"] },
  "command_rules": { "deny": ["\\bheroku\\s+apps:destroy\\b"] }
}
```

`server` is read from this file only if `RUNLEDGER_SERVER` is not set. The environment variable
wins. With the three rule keys, `heroku apps:destroy my-app` is denied (code `rule_deny`),
`mcp__supabase__execute_query` is denied whatever SQL it carries (code `rule_deny`), and
`mcp__foo__obliterate_everything` is destructive (high, so it asks) because you added the word.

### Example: a project file that tightens the guard

```json
{
  "guard": {
    "deny": ["mcp:high"],
    "ask": ["severity:medium"],
    "fail_closed": true,
    "approval": { "timeout_s": 60 }
  },
  "severity_overrides": { "shell_outside": "high" },
  "extra_secret_paths": ["(^|/)config/prod\\.ya?ml$"],
  "command_rules": { "deny": ["\\bnpm\\s+run\\s+release\\b"] },
  "tool_rules": { "ask": ["mcp__vercel__*"] },
  "mcp_verbs": { "destructive": ["obliterate"] }
}
```

With this file, destructive MCP calls are denied, medium findings ask, failures deny, and the
approval wait is 60 seconds. The repository's own rule denies `npm run release`, every Vercel tool
asks, and a tool whose name carries the added word is destructive, so it is denied as well. A
repository cannot set `server`, `api_key_env` or `on_timeout: "ask"`, cannot lower a severity,
cannot add `read` words, and any such value is logged as a warning.

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
masking as the log, described below). Each finding's reason is a display copy: the whole spaced
drive path the command named is shown first, the copy is then masked and cut at 300 characters.
The session id and the folder are sent unchanged. If the team has a Slack or webhook URL set, the
server also notifies it about each new approval, and those messages include the same summary and
findings.

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
- A line with `"decision": "warning"` records a setting the guard ignored: a project setting that
  would loosen the guard, or a rule pattern in either file that is not a safe regular expression.
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
arguments (including the SQL of an execute-class tool) are never logged. Each string is cut at 200
characters.

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
  ignored without a warning. A `command_rules` pattern that the guard cannot compile safely is
  dropped with a warning in the log, and a project `mcp_verbs.read` list, a project exclusion and
  a project severity that would lower a risk are all logged as warnings too.
