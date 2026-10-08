# RunLedger (MVP 0.1)

**A black box recorder for AI coding agents.** RunLedger reads a Claude Code session and turns it into a shareable receipt: what the agent changed, what it ran, which model did each step, what it cost, and what looked risky.

> Early-stage project. This is a first working prototype, not a finished product.

## What you get

- **Receipt** as a single HTML file (also Markdown or JSON)
- **Files changed** with lines added / removed, new and deleted files
- **Every step in plain language**: deterministic by default, or rewritten by Claude Haiku with `--ai`
- **Model, tokens and estimated cost per step** (public Claude API list prices)
- **Risk score 0–100 with reasons**: secrets files read or changed, deleted or skipped tests, writes outside the project folder, `rm -rf`, force-push, `sudo`, `curl | sh`, destructive SQL, package installs, MCP calls
- `--fail-on SCORE` exit code for CI or hooks

No dependencies. Python 3.9+. Works offline unless you use `--ai`.

## Install

```bash
# unzip runledger-mvp.zip (or clone the repo), then:
cd runledger
pip install -e .
```

(Or run without installing: `python -m runledger ...` from this folder.)

## Use

```bash
cd ~/my-project                 # a folder where you used Claude Code
runledger list                  # recent sessions for this folder
runledger receipt --open        # receipt for the latest session, opens in the browser
runledger receipt --format md -o receipt.md
runledger receipt path/to/session.jsonl --format json -o run.json

# plain-language summaries by Claude (costs ~1 cent per run with Haiku)
export ANTHROPIC_API_KEY=sk-ant-...
runledger receipt --ai --open
```

Try it on the bundled sample run:

```bash
runledger receipt tests/fixtures/sample_session.jsonl --open
```

Claude Code keeps sessions in `~/.claude/projects/<project-folder>/<session-id>.jsonl` (or under `$CLAUDE_CONFIG_DIR`).

## Automatic receipt after every run (Claude Code hook)

Add this to `.claude/settings.json` in your project (or `~/.claude/settings.json`):

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          { "type": "command", "command": "runledger receipt -o .runledger/latest.html >/dev/null 2>&1 || true" }
        ]
      }
    ]
  }
}
```

Every time Claude Code finishes, `.runledger/latest.html` is refreshed. Add `.runledger/` to `.gitignore`.

## Real-time guard

`runledger guard` is a Claude Code `PreToolUse` hook. It checks every tool call before it runs, with the same rules as the receipt, and answers **deny**, **ask** or nothing (no opinion).

```bash
pip install -e .                   # puts `runledger` on PATH; the hook runs it
runledger guard install            # this project: .claude/settings.json
runledger guard install --global   # every project: ~/.claude/settings.json
runledger guard test '{"session_id":"s1","cwd":"D:\my-app","tool_name":"Bash","tool_input":{"command":"rm -rf /"}}'
```

`install` merges one entry into `hooks.PreToolUse` and keeps everything else. Before the first change it copies the file to `settings.json.bak`. Running it again changes nothing. If a guard entry already exists, its timeout is updated in place and no second entry is added.

```json
"PreToolUse": [{ "matcher": "*", "hooks": [{ "type": "command", "command": "runledger guard", "timeout": 135 }] }]
```

**Defaults** (used for any list that no file sets):

- **deny**: a hardcoded secret written into a file, and high-severity shell commands (`rm -rf`, force push, `curl | sh`, `DROP TABLE`, `npm publish`, `kubectl apply`). `sudo` is the exception: it asks.
- **ask**: other high-severity risks, such as `sudo`, reading `.env`, writing outside the project or deleting tests.
- **allow**: medium and low risks.

**Two settings files, and they are not equal.**

- `~/.runledger/config.json` is yours. It may set everything: the approval server and key, the lists, the mode.
- `<project>/.runledger.json` belongs to the repository. It may only **tighten** the guard. Each setting in it that would loosen the guard is ignored, and a warning line is written to `guard.log`.

**Your file** (`~/.runledger/config.json`; the home folder is `HOME`, or `USERPROFILE` on Windows):

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

**The project's file** (`<project>/.runledger.json`, section `guard`) adds to your rules:

```json
{
  "guard": {
    "deny": ["mcp"],
    "ask": ["severity:medium"],
    "fail_closed": true,
    "approval": { "timeout_s": 60 }
  }
}
```

What the project file can and cannot do:

- **mode**: `monitor` takes effect only when your file also says `monitor`. A project can switch monitor to `enforce`, never the reverse.
- **fail_closed**: on if either file turns it on.
- **deny and ask**: the project's entries are added to your list (or the default). Nothing is removed. A `!` exclusion in the project file cancels matches only within that same project list, so it cannot remove one of your rules or a default rule.
- **severity**: `severity_overrides` may raise a risk's severity, not lower it. Its `ignore` list is not applied by the guard (the receipt still uses it).
- **approval**: `timeout_s` may come from the project. `on_timeout: "ask"` (a local prompt instead of a deny) would loosen the guard, so only your file can set it.
- **approval server and key**: the project file may not set them (see Approval below).

Entry forms for every list: a risk code (`mcp`), `severity:<high|medium|low>`, `reason:<text>` (a case-insensitive part of the reason), or `<code>:<level>` (`command:high`). A leading `!` is an exclusion from its own list. Invalid entries are ignored. A `deny` or `ask` list in your file replaces the default for that list. The decision is the most restrictive one: any deny beats any ask.

- **mode**: `enforce` (default) or `monitor`. Monitor never blocks: it only logs what it would have done, and it ignores `fail_closed`.

**Approval.** When a call is an *ask* and an approval server is configured, the guard posts the call to `POST {server}/api/approvals` with `Authorization: Bearer <key>`, then polls `GET {server}/api/approvals/{id}` every 2 seconds. Status `approved` allows the call, `denied` or `expired` blocks it, and no answer within `timeout_s` follows `on_timeout` (`deny`, or `ask` to fall back to the local prompt). If the server cannot be reached, the guard shows the normal local prompt.

The server and key come only from the environment (`RUNLEDGER_SERVER`, `RUNLEDGER_API_KEY`) or from your `~/.runledger/config.json`. `api_key_env` names the variable that holds the key. For the server, the environment variable wins over the file. A project's `.runledger.json` can never choose either one, so a cloned repository cannot send your key to a server of its choosing.

`install` writes the hook's `timeout` as `timeout_s` + 15 seconds: 135 s by default, or the project's own `timeout_s` when you install per project. The hook must outlive the approval wait, or its answer is lost. Run `install` again after you change `timeout_s`; it updates the existing entry.

**What the hook prints.** `deny` and `ask` go to Claude Code with a reason; Claude reads a deny reason and adapts. A plain allow prints nothing, so Claude Code's own permission rules still decide. Only an approval from the server prints `allow`, because a hook that answers `allow` skips the user's permission prompts.

**Failures.** An internal error or unreadable input prints nothing and exits 0, so the call goes through Claude Code's normal permissions (fail open). Set `"fail_closed": true` (in either file) to deny instead.

**Log.** Each decision is one JSON line in `<project>/.runledger/guard.log`: time, session, tool, decision, risk codes, and a command or file path. Each project setting that was ignored adds a `warning` line saying what was ignored. Secrets are masked and text is cut at 200 characters. File contents are never logged. The folder is already in `.gitignore`.

## Supported agents

`list`, `receipt` and `push` read each agent's own session logs. Choose one agent
with `--agent`; without it, the newest session from any agent in the folder is used.

| Agent | `--agent` | Where its sessions are |
| --- | --- | --- |
| Claude Code | `claude-code` | `~/.claude/projects/<project>/<session>.jsonl` (or `$CLAUDE_CONFIG_DIR`) |
| Codex CLI | `codex` | `$CODEX_HOME/sessions/YYYY/MM/DD/rollout-*.jsonl` (default `~/.codex`) |
| Aider | `aider` | `.aider.chat.history.md` in the project folder |
| Any other agent | `native` | `<project>/.runledger/runs/*.runledger.json` or `*.runledger.jsonl` |

Any agent can write the RunLedger format, so it needs no adapter. The format is
documented in [docs/format.md](docs/format.md), with a Python and a Node example.

```bash
runledger list --agent native
runledger receipt .runledger/runs/3f9c2e1a.runledger.jsonl --open
```

When an agent reports its own cost, the receipt shows it as "reported by agent"
next to the list-price estimate.

## How the numbers work

- **Cost** = tokens × list price of the model for each assistant message (cache writes at 1.25× input, cache reads at the cache-hit rate). On a Claude subscription you are not billed per token, so read it as "what this run would cost on the API".
- When one model message issues several tool calls, its tokens are split evenly across those steps.
- **Risk score** is rule-based on purpose: every point has a reason you can check. High = 30 pts, Medium = 15, Low = 5, repeats of the same kind count less, capped at 100. Low < 25 ≤ Medium < 60 ≤ High.

## Roadmap

- Hosted share links (one URL per receipt) and team approval
- Risk review and diff explanations by Claude Sonnet / Opus for flagged runs
- Permission rules enforced through Claude Code `PreToolUse` hooks
- One-click rollback (git snapshot before each run)
- MCP and other agents

## Tests

```bash
python -m pytest -q
```

---
RunLedger · founded October 2026 by Claudiu Cojocaru · https://runledger.site · hello@runledger.site
