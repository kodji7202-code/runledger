# The JSON receipt (schema version 1)

`runledger receipt --format json` writes one JSON object per run. Scripts, CI jobs and the team
server read it. Its shape is fixed by a JSON Schema that ships with the package:

```python
from importlib import resources
schema = resources.files("runledger").joinpath("receipt.schema.json").read_text()
```

## Versioning

- `schema_version` is `1`. `runledger_version` is the RunLedger release that wrote the receipt.
- Within a schema version, changes are **additive only**: new keys can appear, and existing keys
  keep their names and types. Removing a key or changing its type bumps `schema_version`.
- Consumers must ignore keys they do not know.
- Markdown receipts written to stdout end with `<!-- runledger: safe=true schema=1 -->`, so the
  mode and schema can be checked in a pasted receipt without changing how it looks.

## Top-level keys

| Key | Type | Meaning |
| --- | --- | --- |
| `schema_version` | integer | `1` |
| `runledger_version` | string | release that wrote the receipt |
| `safe` | boolean | paths were shortened and e-mails masked ([safe mode](quickstart.md#safe-mode)) |
| `session_id` | string | the agent's session id |
| `rollback` | object or null | `checkpoint_id` and `command` when a local checkpoint exists |
| `agent`, `agent_id` | string | display name (`Factory Droid`) and adapter id (`droid`) |
| `cwd` | string or null | the run folder. In safe mode: repository name plus subfolder, such as `app/src` |
| `git_branch` | string or null | branch at the time of the receipt, when the log does not record one |
| `started`, `ended` | string or null | ISO 8601 timestamps from the log |
| `duration_seconds` | number or null | null when a timestamp is missing or unreadable |
| `request` | array of strings | the first three prompts |
| `overview` | string | a short summary of the run |
| `risk` | object | `score` (0-100), `level` (`Low`, `Medium`, `High`), `reasons` |
| `totals` | object | see below |
| `models` | object | `{model id: {"tokens": int}}`. The `unattributed` key holds tokens no single model can be credited with |
| `pricing_note` | string or null | how to add prices, when a model has none |
| `files` | array | changed files, see below |
| `steps` | array | one entry per tool call, see below |
| `quality` | object or null | `score`, `grade`, `signals` ([analysis.md](analysis.md)) |
| `recommendations` | array | cost tips: `kind`, `title`, `detail`, `est_savings_usd`, `steps` |
| `ai_review` | object or null | the `--review` result ([analysis.md](analysis.md#ai-risk-review)) |
| `guard` | array, optional | Guard decisions, present only when the run has some ([guard.md](guard.md#guard-decisions-in-receipts)) |

A risk reason (in `risk.reasons` and `steps[].risks`) has `severity` (`high`, `medium`, `low`),
`code`, `reason` and `step`.

## `totals`

| Key | Type | Meaning |
| --- | --- | --- |
| `steps` | integer | number of steps |
| `tokens`, `input_tokens`, `output_tokens`, `cache_write_tokens`, `cache_read_tokens` | integer | token counts. 0 when usage is unknown; check `usage_source` |
| `cost_usd` | number or null | estimate from list prices. **Null means unknown, never 0** |
| `reported_cost_usd` | number or null | the cost the agent reported itself |
| `cost_partial` | boolean | true when some tokens have no price, so `cost_usd` is a lower bound |
| `unknown_models` | array of strings | models with tokens but no price |
| `files_changed` | integer | length of `files` |
| `usage_source` | string or null | Droid only: `messages`, `settings` or `missing` |
| `guard` | object, optional | counts: `blocked`, `asked`, `approved`, `rejected`, `monitor_blocked`, `monitor_asked` |

## `files[]`

`path` (relative to the run folder when it is inside it), `added`, `removed` (line counts),
`created`, `deleted`, `shell` (written through the shell, so counts may be incomplete),
`verified` (counts come from Git) and `binary` (Git sees a binary file, so there are no counts).

## `steps[]`

| Key | Type | Meaning |
| --- | --- | --- |
| `n` | integer | step number, from 1 |
| `tool` | string | common tool name (`Bash`, `Edit`, `Write`, ...) |
| `summary` | string | one line describing the step |
| `model` | string or null | model that issued the call |
| `tokens` | integer | kept for compatibility; meaningful only when `tokens_known` is true |
| `tokens_known` | boolean | false when no source gave this step tokens |
| `tokens_estimated` | boolean | the tokens are an even split of a session total |
| `cost_usd` | number or null | null when tokens or price are unknown |
| `error` | boolean | the tool reported an error |
| `risks` | array | risk reasons raised by this step |

## Checking a receipt

The test suite validates every fixture against the schema with safe mode on and off
(`tests/test_receipt_schema.py`). Any JSON Schema validator works too, for example:

```bash
runledger receipt --format json -o run.json
python -m jsonschema -i run.json "$(python -c 'import runledger,os;print(os.path.join(os.path.dirname(runledger.__file__),"receipt.schema.json"))')"
```

`jsonschema` is not a RunLedger dependency; install it separately if you want this check.
