# The RunLedger format (version 1)

RunLedger reads the session logs of Claude Code natively. Any other agent can
write this format instead, and then `runledger list`, `receipt`, `push` and the
GitHub comment work for it with no change to RunLedger.

The format is one run per file, in one of two layouts with the same fields.

- **JSON**: one object for the whole run. Use it when the agent writes the file
  once, at the end.
- **JSONL**: one JSON object per line. Use it when the agent writes as it goes.
  A run that stops early still leaves a file RunLedger can read.

## Where the files go

```
<project>/.runledger/runs/<anything>.runledger.json
<project>/.runledger/runs/<anything>.runledger.jsonl
```

- `runledger list`, `runledger receipt --latest` and `runledger push --latest`
  find the files in `<project>/.runledger/runs/` (the folder you run them in, or
  `--project PATH`). Only files directly inside `runs/` are found.
- `runledger receipt PATH` accepts any file. A file whose name ends in
  `.runledger.json` or `.runledger.jsonl` is read as this format. Any other
  `.json` or `.jsonl` file is read as this format when its first JSON object has
  a `runledger_format` key.
- Runs can contain prompts and command output, so add `.runledger/` to your
  project's `.gitignore` unless you want them in git. (RunLedger's own repository
  already ignores it.)
- `runledger github comment --sessions-dir DIR` scores `*.jsonl` and
  `*.runledger.json` files in `DIR`.

## Header fields

The header is the JSON object of the JSON layout, or line 1 of the JSONL layout.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `runledger_format` | integer | yes | Must be `1`. |
| `agent` | string | no | The agent's name, shown as "Agent: …" in receipts. Default `"native"`. |
| `session_id` | string | no | Default: the file name without `.runledger.json` / `.runledger.jsonl`. |
| `cwd` | string or null | no | The project folder. Used by the risk rules to tell paths inside the project from paths outside it. |
| `git_branch` | string or null | no | Shown in the receipt. |
| `started`, `ended` | ISO 8601 string or null | no | For example `"2026-10-08T09:00:00Z"`. The duration is `ended - started`. |
| `prompts` | array of strings | no | What the user asked for. The first one is the title. |
| `final_message` | string | no | The agent's last message to the user. |
| `models` | object | no | Model id to usage totals for the whole run (see below). |
| `reported_cost` | number or null | no | Cost in USD that the agent itself reports. Shown as "reported by agent". RunLedger does not change it. |

Unknown fields are ignored, so newer agents can add fields safely.

### Usage

A usage object has four optional non-negative integers. A missing field counts as 0.

| Field | Meaning |
| --- | --- |
| `input_tokens` | Uncached input tokens. |
| `output_tokens` | Output tokens. |
| `cache_write_tokens` | Tokens written to the prompt cache. |
| `cache_read_tokens` | Tokens read from the prompt cache. |

`models` maps each model id, for example `"claude-sonnet-5-5"`, to a usage
object. The run's token totals come from `models`. If `models` is absent, the
totals are the sum of the steps' `usage`.

Cost is estimated from the built-in price table (`runledger/prices.json`, or
`RUNLEDGER_PRICES`) for every model that has a price. A model without a price
gets no estimate. If the agent gives `reported_cost`, the receipt shows that too.

## Steps

A step is one tool call, in the order it happened.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `tool` | string | yes | Tool name in the canonical vocabulary below. |
| `input` | object | no | The tool's arguments, in the canonical form below. |
| `model` | string or null | no | The model that made this call. If omitted and the run has exactly one model, that model is used. |
| `timestamp` | ISO 8601 string or null | no | When the call happened. |
| `usage` | usage object | no | Tokens for the model call that issued this step. Used for the step's cost. |
| `result_text` | string | no | The tool's output. Only the first 4000 characters are kept. |
| `is_error` | boolean | no | `true` if the tool failed. Default `false`. |

### Canonical tool names

Use these names and input keys so RunLedger's risk rules and receipts work the
same for every agent. Any other tool keeps its own name.

| `tool` | `input` |
| --- | --- |
| `Bash` | `{"command": str}` (any shell) |
| `PowerShell` | `{"command": str}` |
| `Read` | `{"file_path": str}` |
| `Write` | `{"file_path": str, "content": str}` |
| `Edit` | `{"file_path": str, "old_string": str, "new_string": str}` |
| `MultiEdit` | `{"file_path": str, "edits": [{"old_string": str, "new_string": str}]}` |
| `Delete` | `{"file_path": str}` |
| `WebFetch` | `{"url": str}` |
| `Search` | `{"pattern": str, "path": str}` |
| `mcp__<server>__<tool>` | as given |

## JSON layout

One object. Steps are in the `steps` array.

```json
{
  "runledger_format": 1,
  "agent": "my-agent",
  "session_id": "3f9c2e1a",
  "cwd": "/home/dev/app",
  "git_branch": "fix/retry",
  "started": "2026-10-08T09:00:00Z",
  "ended": "2026-10-08T09:02:10Z",
  "prompts": ["Fix the retry test."],
  "final_message": "The retry test passes.",
  "models": {
    "claude-sonnet-5-5": {"input_tokens": 9000, "output_tokens": 700, "cache_write_tokens": 0, "cache_read_tokens": 4000}
  },
  "reported_cost": null,
  "steps": [
    {"tool": "Read", "input": {"file_path": "/home/dev/app/tests/test_retry.py"},
     "model": "claude-sonnet-5-5", "timestamp": "2026-10-08T09:00:12Z",
     "usage": {"input_tokens": 3000, "output_tokens": 200}, "result_text": "def test_retry(): ...", "is_error": false},
    {"tool": "Edit", "input": {"file_path": "/home/dev/app/retry.py",
                               "old_string": "attempts = 1", "new_string": "attempts = 3"},
     "model": "claude-sonnet-5-5", "timestamp": "2026-10-08T09:01:30Z",
     "usage": {"input_tokens": 6000, "output_tokens": 500}, "result_text": "", "is_error": false}
  ]
}
```

## JSONL layout

Line 1 is the header. Each later line is one step, or `{"type": "end", ...}`
on the last line. Blank lines are skipped.

```jsonl
{"runledger_format": 1, "agent": "my-agent", "session_id": "3f9c2e1a", "cwd": "/home/dev/app", "started": "2026-10-08T09:00:00Z", "prompts": ["Fix the retry test."]}
{"tool": "Read", "input": {"file_path": "/home/dev/app/tests/test_retry.py"}, "model": "claude-sonnet-5-5", "timestamp": "2026-10-08T09:00:12Z", "usage": {"input_tokens": 3000, "output_tokens": 200}, "result_text": "def test_retry(): ...", "is_error": false}
{"tool": "Bash", "input": {"command": "python -m pytest -q"}, "model": "claude-sonnet-5-5", "timestamp": "2026-10-08T09:01:05Z", "result_text": "1 failed, 0 passed", "is_error": true}
{"type": "end", "ended": "2026-10-08T09:02:10Z", "final_message": "The retry test still fails.", "models": {"claude-sonnet-5-5": {"input_tokens": 9000, "output_tokens": 700}}, "reported_cost": 0.03}
```

The end record may also repeat any header field (for example `models` or
`reported_cost`) when the agent only knows it at the end. The value in the end
record wins. A step line may carry `"type": "step"`, but it is optional. Nothing
may follow the end record.

## Validation

RunLedger rejects a file that does not follow the format and says why:

- JSONL errors name the file and the line:
  `runs/3f9c.runledger.jsonl: line 3: 'tool' must be a non-empty string`
- JSON errors name the file and the step (counted from 1), or the line where
  the JSON itself is broken:
  `runs/3f9c.runledger.json: step 2: 'usage.input_tokens' must be a non-negative whole number`

Checks: `runledger_format` must be `1`; `tool` must be a non-empty string;
`input`, `usage` and `models` must be objects; token counts must be
non-negative whole numbers (no floats, no booleans); `reported_cost` must be a
finite non-negative number or null; `is_error` must be a boolean; `prompts`
must be an array of strings; `steps` must be an array. Every other string
field must be a string or null.

## Emitting the format

Write the header when the run starts, one step line per tool call as it
happens, and the end record when the run stops. Write UTF-8, one JSON object per
line, with `\n` line endings.

### Python

```python
# runledger_log.py: no dependencies, Python 3.9+
import json
import os
import uuid
from datetime import datetime, timezone


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class RunLedgerLog:
    def __init__(self, project_dir, agent="my-agent", prompt=""):
        self.session_id = uuid.uuid4().hex
        runs = os.path.join(project_dir, ".runledger", "runs")
        os.makedirs(runs, exist_ok=True)
        self.path = os.path.join(runs, self.session_id + ".runledger.jsonl")
        self._file = open(self.path, "w", encoding="utf-8")
        self._write({"runledger_format": 1, "agent": agent, "session_id": self.session_id,
                     "cwd": os.path.abspath(project_dir), "started": _now(), "prompts": [prompt]})

    def _write(self, obj):
        self._file.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self._file.flush()

    def step(self, tool, tool_input, result="", is_error=False, model=None, usage=None):
        self._write({"tool": tool, "input": tool_input, "model": model, "timestamp": _now(),
                     "usage": usage, "result_text": result, "is_error": is_error})

    def finish(self, final_message, models=None, reported_cost=None):
        self._write({"type": "end", "ended": _now(), "final_message": final_message,
                     "models": models or {}, "reported_cost": reported_cost})
        self._file.close()


# Example
log = RunLedgerLog(".", agent="my-agent", prompt="Fix the retry test.")
log.step("Read", {"file_path": "tests/test_retry.py"}, result="def test_retry(): ...",
         model="claude-sonnet-5-5", usage={"input_tokens": 3000, "output_tokens": 200})
log.step("Bash", {"command": "python -m pytest -q"}, result="1 failed, 0 passed", is_error=True)
log.finish("The retry test still fails.",
           models={"claude-sonnet-5-5": {"input_tokens": 9000, "output_tokens": 700}})
```

Then `runledger receipt .runledger/runs/<id>.runledger.jsonl --open`, or
`runledger receipt --agent native`.

### Node

```js
// runledger-log.mjs: no dependencies, Node 18+
import { mkdirSync, appendFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { randomUUID } from "node:crypto";

const now = () => new Date().toISOString().replace(/\.\d{3}Z$/, "Z");

export class RunLedgerLog {
  constructor(projectDir, { agent = "my-agent", prompt = "" } = {}) {
    this.sessionId = randomUUID();
    const runs = join(projectDir, ".runledger", "runs");
    mkdirSync(runs, { recursive: true });
    this.path = join(runs, `${this.sessionId}.runledger.jsonl`);
    this.write({ runledger_format: 1, agent, session_id: this.sessionId,
                 cwd: resolve(projectDir), started: now(), prompts: [prompt] });
  }

  write(obj) {
    appendFileSync(this.path, JSON.stringify(obj) + "\n", "utf8");
  }

  step(tool, input, { result = "", isError = false, model = null, usage = null } = {}) {
    this.write({ tool, input, model, timestamp: now(), usage, result_text: result, is_error: isError });
  }

  finish(finalMessage, { models = {}, reportedCost = null } = {}) {
    this.write({ type: "end", ended: now(), final_message: finalMessage, models, reported_cost: reportedCost });
  }
}

// Example
const log = new RunLedgerLog(".", { agent: "my-agent", prompt: "Fix the retry test." });
log.step("Read", { file_path: "tests/test_retry.py" }, {
  result: "def test_retry(): ...", model: "claude-sonnet-5-5",
  usage: { input_tokens: 3000, output_tokens: 200 } });
log.step("Bash", { command: "python -m pytest -q" }, { result: "1 failed, 0 passed", isError: true });
log.finish("The retry test still fails.",
  { models: { "claude-sonnet-5-5": { input_tokens: 9000, output_tokens: 700 } } });
```

## Versions

`runledger_format` is `1`. A future incompatible change would use `2`. RunLedger
rejects a version it does not know, with a message saying which version it reads.
