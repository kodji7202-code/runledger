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
