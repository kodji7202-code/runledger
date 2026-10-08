# Supported agents

RunLedger reads each agent's own session files and turns them into the same receipt. The adapter
for each agent converts its log into common steps, so the risk rules and the receipt work the same
way for every agent.

| Agent | `--agent` value | Status | Where sessions are |
| --- | --- | --- | --- |
| Claude Code | `claude-code` | Stable | `~/.claude/projects/<encoded project folder>/<session>.jsonl` |
| Codex CLI | `codex` | Beta | `$CODEX_HOME/sessions/YYYY/MM/DD/rollout-*.jsonl` (default `~/.codex`) |
| Aider | `aider` | Beta | `.aider.chat.history.md` in the project folder |
| Any other agent | `native` | Open format | `<project>/.runledger/runs/*.runledger.json` or `*.runledger.jsonl` |

Any agent can write the RunLedger format, so an agent does not need an adapter to get receipts,
risk scores and pushes. The format is specified in [format.md](format.md), with a Python and a
Node example.

## Choosing sessions

`list`, `receipt` and `push` all accept `--agent`. Without it, they use the newest session of any
agent in the folder, by file modification time. With it, only that agent's sessions count.

```bash
runledger list                              # every agent, this folder
runledger list --agent codex                # Codex sessions for this folder
runledger receipt --agent aider -o aider.html
runledger push --agent claude-code --server https://runledger.example.com --key KEY
```

- **Folder.** The folder is the current folder unless you pass `--project PATH` (for `list`,
  `receipt` and `push`).
- **All folders.** `runledger list --all` lists sessions from every folder. It covers Claude Code
  and Codex only. Aider keeps no global index, and the RunLedger format lives inside each project,
  so `--all` does not list them. Use `--project` with those agents.
- **An explicit file.** Pass the session file as the argument to skip the folder search:
  `runledger receipt path/to/session.jsonl`. RunLedger detects the agent from the file. If you also
  pass `--agent`, it prints `note: --agent is ignored when a session file is given`.
- **Limit.** `list` shows 20 sessions by default. Use `--limit N` for more.

Detection for an explicit file uses the file itself: a Codex rollout file, an Aider history file,
a RunLedger-format file, or else Claude Code's JSONL format.

## Claude Code (stable)

Claude Code writes one JSONL file per session under `~/.claude/projects/`. Each project folder has
an encoded name: every character of the absolute folder path that is not a letter or digit becomes
`-`. For example, `D:\runledger` becomes `D--runledger`, and `/home/dev/my.app` becomes
`-home-dev-my-app`.

- **Config directory.** If `CLAUDE_CONFIG_DIR` is set, RunLedger reads `$CLAUDE_CONFIG_DIR/projects`
  instead.
- **Matching.** `--project` (or the current folder) is encoded the same way to find its directory.
- **Usage.** Streamed messages that repeat the same message id are counted once.
- **Cost.** Estimated from list prices (see below). Claude Code does not report its own cost.

## Codex CLI (beta)

Codex writes one rollout file per session:

```
$CODEX_HOME/sessions/YYYY/MM/DD/rollout-<timestamp>-<uuid>.jsonl
```

`CODEX_HOME` defaults to `~/.codex`. RunLedger reads the current rollout format and the older one,
which has no envelope on each line.

- **Matching.** A session belongs to a folder when the working directory recorded in the session
  equals that folder.
- **Usage.** The run total is the last cumulative token count in the session. Each model call's
  usage is split evenly across the steps issued since the previous count.
- **Cost.** Estimated from list prices. Codex does not report its own cost.

## Aider (beta)

Aider appends each chat to `.aider.chat.history.md` in the project root. Each session starts with
a line `# aider chat started at YYYY-MM-DD HH:MM:SS`.

- **One receipt per file.** RunLedger reads the **last** session in the history file. Earlier
  sessions in the same file are not listed or scored.
- **Matching.** Only the project's own history file counts. Aider has no global index.
- **Steps.** SEARCH/REPLACE blocks become edits. A block with an empty SEARCH section becomes a
  write. Lines Aider prints for added files become reads. Lines `> Running <command>`, and the
  `/run` and `/test` commands, become shell steps. Commits become `GitCommit` steps, which keep their
  name because they are not part of the common vocabulary. Housekeeping commands such as `/add`,
  `/drop` and `/model` are not counted as steps.
- **Cost.** Aider prints its session cost, and RunLedger shows it as "reported by agent" next to
  the list-price estimate.

## Any other agent: the RunLedger format (open format)

Any agent can write a session file in the RunLedger format and get a receipt, a risk score and a
push without any code in RunLedger.

```
<project>/.runledger/runs/<name>.runledger.json     one JSON object with the header, prompts and steps
<project>/.runledger/runs/<name>.runledger.jsonl    a header line, then one step per line, optional end line
```

- **Matching.** Files are read from `<project>/.runledger/runs/` (directly inside it). Without a
  project, the format is not listed.
- **Validation.** A file that does not validate fails with its file name and the line (JSONL) or
  step (JSON) number.
- **Cost.** A `reported_cost` value in the file is shown as "reported by agent". Otherwise the cost
  is estimated from list prices.
- **Explicit files.** A `.json` or `.jsonl` file whose first object has a `runledger_format` key is
  detected as this format when passed on the command line.

The field-by-field specification is [format.md](format.md).

## Costs across agents

Costs are estimates from the public Claude API list prices in `runledger/prices.json`, which was
checked on 2026-10-08. Each message's tokens are multiplied by the list price of its model. Cache
writes are priced at 1.25 times the input price, and cache reads at the cache-hit rate. A model
RunLedger does not know shows `n/a` rather than a guess.

To override prices, point `RUNLEDGER_PRICES` at a JSON file with the same shape as `prices.json`.
The file adds or replaces entries; everything else keeps the built-in values.

On a Claude subscription you are not billed per token. Read the figure as "what this run would cost
on the API".

Where an agent reports its own cost (Aider, and the RunLedger format when the file includes
`reported_cost`), the receipt shows the reported figure next to the estimate.
