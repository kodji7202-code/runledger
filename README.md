# RunLedger

**Receipts and safe rollback for AI coding agents.**

RunLedger reads the session logs of your coding agents (Claude Code, Codex CLI, Factory Droid and
Aider) and turns each run into a receipt: what changed, what ran, which model did each step, what it
cost, and what looked risky. A Claude Code hook can stop risky calls before they run, and a small team
server collects receipts from the whole team.
Local Git checkpoints let you restore files from before a run, with a safety checkpoint for undo.

RunLedger is fully free and open source, including the self-hosted team server.
Version 0.6.0. Python 3.9 or later, no runtime dependencies.

## Quickstart

```bash
pip install runledger-ai              # or `pip install .` from a clone of this repository
cd ~/my-project                    # a folder where you ran a coding agent
runledger receipt --open           # latest session as an HTML receipt, opened in your browser
```

The receipt is saved as `runledger-<first 8 characters of the session id>.html` in the current folder.
More in [docs/quickstart.md](docs/quickstart.md).

## What you get

- **Receipts** as HTML, Markdown or JSON: files changed with lines added and removed, every step in
  plain language, the model, tokens and estimated cost for each step, and a risk score from 0 to 100
  with the reason for each finding. Known secret formats are masked.
- **Safe rollback:** snapshot tracked and non-ignored untracked files without changing HEAD, the
  index or stash. Preview changes, confirm a restore, and undo with an automatic safety checkpoint.
  See [docs/rollback.md](docs/rollback.md).
- **A quality score** (grades A to F) with the signals behind it, and **cost tips** with estimated
  savings, in every receipt. See [docs/analysis.md](docs/analysis.md).
- **An AI risk review** (`--review`), optional: Claude marks each finding as confirmed, false
  positive or uncertain. The rule-based score does not change.
- **Summaries with Claude** (`--ai`), optional. This sends session content to Anthropic. See
  [Privacy](#privacy).
- **A CI gate:** `runledger receipt --fail-on 60` exits with code 2 when the score is 60 or more.
- **A real-time guard** for Claude Code. It denies, asks or allows each tool call, and it can route
  "ask" decisions to a person on the team server. See [docs/guard.md](docs/guard.md).
- **A team server:** a shared dashboard, API keys with roles, an audit log, approvals with Slack and
  webhook notifications, team budgets with alerts, and compliance exports. See
  [docs/enterprise.md](docs/enterprise.md) and [docs/api.md](docs/api.md).
- **Pull request receipts:** one sticky comment per pull request, updated on every push. See
  [docs/github.md](docs/github.md).

## Supported agents

| Agent | `--agent` | Status | Reads sessions from |
| --- | --- | --- | --- |
| Claude Code | `claude-code` | Stable | `~/.claude/projects/<project>/` |
| Codex CLI | `codex` | Beta | `$CODEX_HOME/sessions/` (default `~/.codex`) |
| Factory Droid | `droid` | Beta | `$FACTORY_HOME/sessions/<encoded project folder>/` (default `~/.factory`) |
| Aider | `aider` | Beta | `.aider.chat.history.md` in the project folder |
| Any agent | `native` | Open format | `<project>/.runledger/runs/` |

Any agent can write the RunLedger format and get receipts, risk scores and pushes without an adapter.
The specification is in [docs/format.md](docs/format.md). Details per agent are in
[docs/agents.md](docs/agents.md).

## Checkpoint, restore, undo

```bash
runledger checkpoint --label "Before agent run"       # prints a checkpoint id
runledger checkpoints                               # list snapshots and differing files
runledger rollback CHECKPOINT_ID --dry-run           # preview restored and removed files
runledger rollback CHECKPOINT_ID                     # confirm, save safety checkpoint, restore
runledger rollback SAFETY_CHECKPOINT_ID               # undo using the id printed by rollback
```

Requires Git, but no Git identity setup. Ignored untracked files stay untouched.
Rollback never changes HEAD, branches, staging, or stashes. Scripts must pass `--yes` to restore;
use `--dry-run` to preview without restoring.
Use `--run SESSION_ID` when creating a checkpoint to include its rollback command in the session
receipt. `guard install` also installs automatic Claude Code session-start and pre-prompt
checkpoints. [docs/rollback.md](docs/rollback.md) covers Droid hooks, Codex, pruning and limits.

## Team server in five steps

```bash
runledger team create myteam --db runledger.db       # 1. create the team; prints its API key once
runledger serve --db runledger.db                    # 2. start the server on http://127.0.0.1:8787
# 3. open http://127.0.0.1:8787/?key=YOUR_KEY once, then use the plain address
export RUNLEDGER_SERVER=http://127.0.0.1:8787        # 4. on each developer's machine
export RUNLEDGER_API_KEY=YOUR_KEY
runledger push --user "ana@example.com"              # 5. push the latest run
```

For a shared server, serve it over HTTPS: [docs/self-hosting.md](docs/self-hosting.md) has a Docker
Compose setup with automatic certificates and a bare-metal setup behind nginx.
[docs/enterprise.md](docs/enterprise.md) covers roles, key rotation and the audit log.

## Real-time guard in three commands

```bash
runledger guard install                              # 1. this project: .claude/settings.json
runledger guard test '{"session_id":"s1","cwd":"/work/my-app","tool_name":"Bash","tool_input":{"command":"rm -rf /"}}'   # 2. see a decision
runledger guard install --global                     # 3. or every project: ~/.claude/settings.json
runledger guard uninstall                            # remove it again (--global for ~/.claude)
```

By default the guard **denies** a hardcoded secret written into a file and high-severity commands such
as `rm -rf`, force pushes, `curl | sh`, `DROP TABLE`, `npm publish` and `kubectl apply`. It **asks**
before `sudo`, reading a `.env` file, writing outside the project, or deleting a test. Everything else
is allowed. Two settings files control it: your own `~/.runledger/config.json` may set anything, and a
project's `.runledger.json` may only make the guard stricter. See [docs/guard.md](docs/guard.md).

## GitHub Action

Post a receipt on every pull request. Commit the session files you want reviewed to
`.runledger/sessions/`: Claude Code `.jsonl` transcripts, Codex rollouts, Factory Droid transcripts
(with their `.settings.json` sidecar, which carries the token usage), or `.runledger.json` written by
any agent. Then add this workflow:

```yaml
name: RunLedger receipt
on: pull_request
permissions:
  contents: read
  pull-requests: write
jobs:
  receipt:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: kodji7202-code/runledger@v0.6.0
        with:
          fail-on: "60"                   # the job fails at 60 or above; "" never fails
```

Details, inputs and the exit codes are in [docs/github.md](docs/github.md).

## Privacy

RunLedger is local first. `receipt`, `list` and the guard run on your machine. RunLedger has no
telemetry. The network calls it makes are the ones you ask for: pushing to your team server, the
guard's approval requests to the server you configure, webhooks you set, the GitHub API in the
action, and the Anthropic API only when you use `--ai` or `--review`.

What a receipt contains:

- the prompts (the first three, in full, in the JSON receipt);
- each step: the first line of a command (up to 120 characters), file paths and line counts, search
  patterns and URLs, and the step's model, tokens and cost;
- the risk findings and their reasons;
- Guard decisions when available: blocked calls, user and server approvals or rejections,
  monitor-only outcomes, and summary counts. See [the Guard log](docs/guard.md#guard-decisions-in-receipts).

A receipt does **not** contain file contents or command output (only test pass and fail counts).
**Secrets are masked** in everything a receipt shows, and so in what `push` sends and in the PR
comment: known token formats (Anthropic, OpenAI, AWS, GitHub, GitLab, Slack, Stripe, Google, npm,
Hugging Face, RunLedger keys, JWTs, `Bearer` and `Basic` values, private-key headers), assignments such
as `API_KEY=…`, `password="…"` and `?access_token=…`, flags such as `--token …`, and passwords in URLs.
Masking is pattern-based, so an unusual secret can still get through. Treat a receipt like the
session transcript it came from.

**Safe mode** is on by default for receipts written inside a Git repository (HTML included) and
for Markdown and JSON written to stdout, and always on for the PR comment: local paths are
shortened (repository-relative, `~` for the home folder) and e-mail addresses are masked. A
receipt written outside a repository keeps full paths unless `--safe` is passed. Use `--safe` or
`--no-safe` to choose. See [docs/quickstart.md](docs/quickstart.md#safe-mode).

- **`--ai`** sends the prompts, each step's inputs (up to 600 characters per field, which can include
  file contents being written), each tool result (up to 400 characters) and the agent's final message to
  `api.anthropic.com`. Do not use it on sessions you may not share with that service.
- **`--review`** sends prompts, commands, relative file paths, edit snippets and the rule findings to
  Claude Sonnet for an explanation of each risk. Tool output is not sent. See
  [docs/analysis.md](docs/analysis.md#ai-risk-review).
- **Both mask before sending, with no opt-out:** e-mail addresses become `[EMAIL]`, home folders
  (`C:\Users\<name>`, `/Users/<name>`, `/home/<name>`) become `~...`, and known secret formats become
  `[REDACTED]`. Masking is pattern-based, so an unusual secret can still get through.
- **The guard log** (`.runledger/guard.log`) records tool names, commands and paths, masked the same
  way. Add `.runledger/` to your `.gitignore`.
- **Checkpoints** store real file contents locally in Git, without secret masking. Ignore sensitive
  untracked files before enabling automatic checkpoints. Tracked secrets remain included. Do not
  share checkpoint refs without reviewing their contents.
- **The team server** stores every pushed receipt, including its HTML, in one SQLite file. Anyone with a
  team key can read all of that team's runs. Keys are stored only as hashes. By default nothing is
  deleted automatically; `RUNLEDGER_RETENTION_DAYS` enables automatic retention. See
  [docs/self-hosting.md](docs/self-hosting.md#configuration).

## Model cost estimates

Cost figures estimate LLM token usage from public API list prices, not a charge for RunLedger.
Read them as "what this run would cost on the API". Optional `--ai` and `--review` calls use
your Anthropic API key and may incur charges from Anthropic.

## Documentation

- [Quickstart](docs/quickstart.md): install, first receipt, summaries, CI, hooks, a local team server
- [Supported agents](docs/agents.md): where each agent's sessions are, and `--agent`
- [Session format](docs/format.md): the open format for any agent
- [JSON receipt](docs/receipt-json.md): the versioned receipt contract and its JSON Schema
- [Real-time guard](docs/guard.md): policy files, approvals, fail-open and fail-closed, the log
- [Rollback](docs/rollback.md): Git checkpoints, previews, safety checkpoints, undo and agent hooks
- [Analysis](docs/analysis.md): the quality score, cost tips and the AI risk review
- [Enterprise guide](docs/enterprise.md): roles, API keys, audit, budgets, exports, HTTPS
- [HTTP API](docs/api.md): every endpoint, with examples and error codes
- [Self-hosting](docs/self-hosting.md): Docker Compose, bare metal, backups, upgrades
- [GitHub pull requests](docs/github.md): the action and the comment format
- [Contributing](CONTRIBUTING.md)

## Development

```bash
python -m pip install -e . pytest
python -m pytest -q
```

## License and security

RunLedger is licensed under the Apache License 2.0; see [LICENSE](LICENSE). To report a security
problem, email **hello@runledger.site** and do not open a public issue. See [SECURITY.md](SECURITY.md).

---
RunLedger · [runledger.site](https://runledger.site) · hello@runledger.site
