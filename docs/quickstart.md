# Quickstart

This page takes you from install to a first receipt, then to the optional parts: plain-language
summaries, a CI gate, the real-time guard, and a team server. Every command here was run against
this release.

## 1. Install

RunLedger needs Python 3.9 or later and has no runtime dependencies.

```bash
pip install runledger-ai            # from PyPI
```

From a clone of this repository:

```bash
pip install .                    # or `pip install -e .` if you are developing RunLedger
```

You can also run it without installing, from the repository folder: `python -m runledger ...`.
Check the install with `runledger --version`.

## 2. Your first receipt

A receipt is a record of one agent session: the prompts, the steps the agent took, the files it
changed, the models and cost, and the risk findings with their reasons.

```bash
cd ~/my-project                  # a folder where you ran a coding agent
runledger list                   # recent sessions for this folder, newest first
runledger receipt --open         # latest session as HTML, opened in your browser
```

By default the receipt is written to the current folder as `runledger-<first 8 characters of the
session id>.html`. Pick another name with `-o`:

```bash
runledger receipt -o review.html
runledger receipt --format md -o review.md     # Markdown
runledger receipt --format json -o run.json    # JSON, for scripts and the team server
runledger receipt --format json -o -           # JSON to stdout
```

Choose a session explicitly with a file path:

```bash
runledger receipt path/to/session.jsonl --open
```

Try it on the sample session that ships with the repository:

```bash
runledger receipt tests/fixtures/sample_session.jsonl --open
```

> **Windows note.** `-o -` writes to stdout. When stdout is redirected, a receipt that contains
> characters outside the console code page (common in Markdown) can fail with `UnicodeEncodeError`.
> Set `PYTHONUTF8=1` for that command, or write to a file with `-o`.

Where each agent keeps its sessions, and how to pick one with `--agent`, is in
[agents.md](agents.md).

## 3. Plain-language summaries (optional)

The default receipt uses rule-based wording and never leaves your machine. With `--ai`, Claude
rewrites each step in plain language and writes an overview:

```bash
export ANTHROPIC_API_KEY=sk-ant-...      # PowerShell: $env:ANTHROPIC_API_KEY = "sk-ant-..."
runledger receipt --ai --open
```

- The default model is `claude-haiku-4-5`. Change it with `--ai-model MODEL` or the environment
  variable `RUNLEDGER_SUMMARY_MODEL`.
- **This sends session content to Anthropic.** The prompts, each step's inputs (up to 600
  characters per field, which can include file contents being written), each tool result (up to
  400 characters) and the agent's final message (up to 1,500 characters) go to
  `api.anthropic.com`. Do not use `--ai` on sessions you may not send to that API.
- If the request fails, RunLedger still writes the deterministic receipt and prints
  `AI summaries skipped: ...`.

## 4. Fail a CI job on risk

`--fail-on SCORE` makes `receipt` exit with code 2 when the risk score (0 to 100) is at or above
`SCORE`. Other failures exit with code 1.

```bash
runledger receipt --fail-on 60 -o receipt.html
echo $?                          # 2 if the score is 60 or more
```

To post receipts on pull requests instead, see [github.md](github.md).

## 5. A receipt after every Claude Code run (optional)

Add a `Stop` hook to `.claude/settings.json` in the project, or to `~/.claude/settings.json` for
every project. The folder must exist before `receipt -o` writes into it, so the hook creates it
first:

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          { "type": "command", "command": "mkdir -p .runledger && runledger receipt -o .runledger/latest.html >/dev/null 2>&1 || true" }
        ]
      }
    ]
  }
}
```

The command uses POSIX shell syntax. On Windows, change the redirection to match the shell that
Claude Code uses. Add `.runledger/` to the project's `.gitignore`: receipts contain prompts and
file paths.

## 6. The real-time guard (optional)

The guard is a Claude Code `PreToolUse` hook. It checks each tool call before it runs and answers
allow, ask or deny.

```bash
runledger guard install                   # this project: .claude/settings.json
runledger guard test '{"session_id":"s1","cwd":"/work/my-app","tool_name":"Bash","tool_input":{"command":"rm -rf /"}}'
```

The second command prints `"permissionDecision": "deny"` for the `rm -rf` call. Everything about
policy files, approvals, fail-open and fail-closed, monitor mode and the log is in
[guard.md](guard.md).

## 7. A team server (local trial)

The team server collects receipts that developers push, stores them in one SQLite file, and serves
a shared dashboard.

```bash
runledger team create myteam --db runledger.db     # prints the team's API key once
runledger serve --db runledger.db                  # listens on http://127.0.0.1:8787
```

Keep the key and open the dashboard once with it: `http://127.0.0.1:8787/?key=YOUR_KEY`. The
server sets a session cookie and redirects, so the key leaves the address bar.

Push a run from a developer's machine:

```bash
export RUNLEDGER_SERVER=http://127.0.0.1:8787
export RUNLEDGER_API_KEY=YOUR_KEY
runledger push --user "ana@example.com"
```

PowerShell uses `$env:RUNLEDGER_SERVER = "http://127.0.0.1:8787"` and the same for the key.
`--user` is the developer name shown on the dashboard. Without it, RunLedger uses the git
`user.email`, then the operating-system user.

A local server is for trying things out. For a shared server, use HTTPS: see
[self-hosting.md](self-hosting.md) for Docker Compose with automatic certificates, or a bare-metal
setup behind nginx. Then read [enterprise.md](enterprise.md) for roles, keys and the audit log.

## Next steps

- [agents.md](agents.md): which agents are supported, where their sessions are, and `--agent`.
- [guard.md](guard.md): policy files, approvals and the guard log.
- [enterprise.md](enterprise.md): roles, API keys, audit, budgets, exports and TLS.
- [api.md](api.md): every HTTP endpoint of the team server.
- [format.md](format.md): the open session format, for agents without an adapter.
- [github.md](github.md): risk receipts on pull requests.
