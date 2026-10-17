# Supported agents

RunLedger reads each agent's own session files and turns them into the same receipt. The adapter
for each agent converts its log into common steps, so the risk rules and the receipt work the same
way for every agent.

| Agent | `--agent` value | Status | Where sessions are |
| --- | --- | --- | --- |
| Claude Code | `claude-code` | Stable | `~/.claude/projects/<encoded project folder>/<session>.jsonl` |
| Codex CLI | `codex` | Beta | `$CODEX_HOME/sessions/YYYY/MM/DD/rollout-*.jsonl` (default `~/.codex`) |
| Factory Droid | `droid` | Beta | `$FACTORY_HOME/sessions/<encoded project folder>/<session>.jsonl` (default `~/.factory`) |
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
runledger receipt --agent droid --open
runledger push --agent claude-code --server https://runledger.example.com --key KEY
```

- **Folder.** The folder is the current folder unless you pass `--project PATH` (for `list`,
  `receipt` and `push`).
- **All folders.** `runledger list --all` lists sessions from every folder. It covers Claude Code,
  Codex and Factory Droid. Aider keeps no global index, and the RunLedger format lives inside each project,
  so `--all` does not list them. Use `--project` with those agents.
- **An explicit file.** Pass the session file as the argument to skip the folder search:
  `runledger receipt path/to/session.jsonl`. RunLedger detects the agent from the file. If you also
  pass `--agent`, it prints `note: --agent is ignored when a session file is given`.
- **Limit.** `list` shows 20 sessions by default. Use `--limit N` for more.

Detection for an explicit file uses the file itself: a Codex rollout file, a Droid `session_start`
header, an Aider history file, a RunLedger-format file, or else Claude Code's JSONL format.

## Claude Code (stable)

Claude Code writes one JSONL file per session under `~/.claude/projects/`. Each project folder has
an encoded name: every character of the absolute folder path that is not a letter or digit becomes
`-`. For example, `D:\runledger` becomes `D--runledger`, and `/home/dev/my.app` becomes
`-home-dev-my-app`. When the sanitized name exceeds 200 characters, Claude Code truncates to 200
characters and appends `-<base36(abs(hash))>` (32-bit signed hash over the UTF-16 code units of the
unsanitized path).

- **Config directory.** If `CLAUDE_CONFIG_DIR` is set, RunLedger reads `$CLAUDE_CONFIG_DIR/projects`
  instead.
- **Matching.** `--project` (or the current folder) is encoded the same way to find its directory,
  with a 200-character prefix scan fallback for long project paths.
- **Sub-agents.** Sub-agent transcripts under `<project>/<session>/subagents/` (including nested
  `workflows/<wf>/agent-*.jsonl` and legacy `<project>/subagents/agent-*.jsonl`) are merged into
  the parent session's token usage and cost (deduplicating message ids), with a receipt note
  `includes N sub-agent transcript(s)`. Sub-agent files are not listed as separate sessions.
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

## Factory Droid (beta)

Droid writes a JSONL transcript and a sibling settings file:

```
~/.factory/sessions/<cwd-with-path-separators-replaced-by-dashes>/<session-id>.jsonl
~/.factory/sessions/<cwd-with-path-separators-replaced-by-dashes>/<session-id>.settings.json
```

- **Config directory.** `FACTORY_HOME` overrides `~/.factory`.
- **Relative `cwd`.** `droid exec --cwd .` records `cwd` as `.`. RunLedger recovers the real folder
  from, in order: the session's own `% pwd` context, a folder field in the settings file, the Git
  work tree that a majority of the absolute tool paths lie in, and the encoded session folder name.
  A `-` in that name can be a separator or part of a folder name, so it is decoded against the
  filesystem and accepted only when the folder exists. The recorded relative path is then
  resolved against the recovered folder. JSON receipts record the source as `cwd_source`
  (`header`, `session`, `settings`, `session_dir` or `unknown`). When nothing works, the folder
  stays unknown and is never resolved against the folder RunLedger runs in: the branch is unknown,
  Git line counts are skipped, paths are not flagged as outside the working folder, and safe
  receipts shorten absolute paths to `…/<last two parts>`.
- **Matching.** The `cwd` in the `session_start` header identifies the project. When it is
  relative, the session folder name (the project path with separators replaced by `-`) is used. Sessions are
  sorted newest first by transcript modification time. `list --all --agent droid` covers all projects.
- **Messages.** Human text becomes the request; system reminders and hidden context are excluded.
  Assistant `tool_use` blocks become steps, matched to `tool_result` blocks by id. `Execute` maps
  to `Bash`, `Create` to `Write`, and `Edit` fields to `old_string` / `new_string`. File searches
  map to `Search`; `FetchUrl` maps to `WebFetch`. Patch calls become edits, writes or deletions.
- **Model.** Each assistant message's `modelId` (or `model`) takes precedence over the settings
  file's `model`. No model is guessed when both are absent.
- **Usage.** Per-message usage is counted once when present. The settings file's cumulative
  `tokenUsage` supplies any remaining input, output, cache-creation and cache-read tokens.
  Thinking tokens, credits and `lastCallTokenUsage` are not added again.
  Without per-message usage, a single-model total is split evenly across steps as an approximation.
  A mixed-model cumulative total cannot be attributed reliably, so its remainder is listed as
  `unattributed`, which has no price.
- **Sub-agents.** `inclusiveTokenUsage` minus the session's own `tokenUsage` is sub-agent usage.
  Each child session in `childInclusiveTokenUsageBySessionId` is credited to the model in its own
  settings file when that file is found; the rest is `unattributed`. An inclusive total smaller
  than the session's own usage is ignored.
- **Settings file lookup.** RunLedger reads the sibling `<name>.settings.json` first. If the
  transcript was copied or renamed without it, RunLedger looks for `<session-id>.settings.json`
  under `$FACTORY_HOME/sessions/`, preferring the folder for the session's `cwd`.
- **Where usage came from.** JSON receipts include `totals.usage_source`: `messages` (per-message
  usage), `settings` (the settings file's total) or `missing`, and `tokens_estimated` on each step
  that received an even split. Markdown and HTML receipts add a footer note for estimated
  per-step tokens. When no usage is found, they show token counts as `n/a`, not 0, with a note
  naming the settings file Droid would have written. Other agents leave `usage_source` null.
- **Cost.** Only token counts with a known model price produce estimates. Missing usage, or no
  priced model at all, leaves the run cost unknown. When some tokens are priced and some are not,
  the cost is a lower bound: `≥ $X (1 model unpriced)`, with `totals.cost_partial` in JSON. Missing or partially written settings do not prevent
  parsing the transcript.

## Aider (beta)

Aider appends each chat to `.aider.chat.history.md` in the project root. Each session starts with
a line `# aider chat started at YYYY-MM-DD HH:MM:SS`.

- **One receipt per file.** RunLedger reads the **last** session in the history file. Earlier
  sessions in the same file are not listed or scored.
- **Matching.** RunLedger checks `--project` (or the current folder) and walks up parent folders
  to the Git top-level directory (nearest `.aider.chat.history.md` wins, never escaping above the
  Git top level). Aider has no global index, so `--all` does not list Aider history files.
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

## Checkpoints across agents

`checkpoint`, `checkpoints` and `rollback` work with every agent in a Git working tree.
`guard install` adds automatic Claude Code session-start and pre-prompt checkpoints. Droid can
use the same lifecycle command in `.factory/hooks.json`. Codex and other agents can use manual
commands or their own wrappers. See [rollback.md](rollback.md) for examples and safety limits.

Associate a checkpoint with the transcript's session id using `checkpoint --run SESSION_ID`;
the matching receipt then includes a local rollback command. Checkpoint contents are not uploaded.

## Costs across agents

Costs are estimates from the public Anthropic, OpenAI and Google Gemini API list prices in
`runledger/prices.json`, which was checked on 2026-10-16. Each message's tokens are multiplied by
the list price of its model. Standard 5-minute cache writes are priced at 1.25 times the input
price (or `cache_write_5m`), 1-hour cache writes (`cache_creation.ephemeral_1h_input_tokens`) at
2.0 times the input price (or `cache_write_1h`), and cache reads at the cache-hit rate. Where a
model defines a `long_context` tier, requests whose prompt tokens exceed `threshold_tokens` are
billed at the long-context rates for that request (when only cumulative settings usage is available
in a Droid run, RunLedger bills the base tier and adds a receipt note). When the bundled `checked`
date is more than 120 days old, receipts include a staleness note. A model RunLedger does not know
shows `n/a` rather than a guess, with a short line explaining how to add its price. JSON receipts
expose that guidance as `pricing_note`.

### User prices

Create `~/.runledger/prices.json`, or set `RUNLEDGER_PRICES=/path/to/prices.json` to choose a
different file. The environment variable takes precedence over the default user file.
The selected file adds or replaces entries over the bundled table; other bundled entries stay.
An invalid or unreadable file prints a warning to stderr once per process and is ignored in full. A missing
default user file is normal and silent. Prices load when RunLedger starts.

Use the same schema as the bundled table: `"family:version"` keys and USD per million tokens
for `input`, `output`, and `cache_read` (with optional `cache_write_5m`, `cache_write_1h`,
`long_context`, `valid_from`, `valid_until`, and `windows`). All rates must be finite,
non-negative numbers.
For example, this entry uses the bundled Sonnet 4.5 list prices:

```json
{
  "sonnet:4.5": {"input": 3.0, "output": 15.0, "cache_read": 0.3}
}
```

For `gpt-5-codex`, add a `"gpt:5-codex"` entry with rates from your provider, not guessed rates.
The optional top-level `checked` date is used for the 120-day staleness note in receipts.

Lookup ignores case, provider prefixes such as `anthropic/`, `openai/`, `models/`, `custom:` and
Bedrock `us.anthropic.`, bracket context tags such as `[1m]`, proxy tags such as `-[Proxy]-0`,
dated suffixes (`-20250929` or `-2025-09-29`), deployment suffixes such as `-v1:0`, and `-latest`.
Claude's dotted and legacy names also match, so `anthropic/Claude-Sonnet-4.5-latest`,
`claude-opus-4-6[1m]` and `claude-sonnet-4-5-20250929` all match their canonical keys. Meaningful
or unverified variants such as `-fast`, unverified `-preview`, `-chat-latest`, `-sol`, `-codex`,
and `-mini` keep distinct keys so normalization never guesses a missing price.

On a Claude subscription you are not billed per token. Read the figure as "what this run would cost
on the API".

Where an agent reports its own cost (Aider, and the RunLedger format when the file includes
`reported_cost`), the receipt shows the reported figure next to the estimate.
