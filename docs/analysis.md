# Analysis: quality, cost and AI review

RunLedger adds three layers on top of the rule-based risk score. The first two are
deterministic and run locally on every receipt. The third is optional and calls Claude.

## Quality score

`runledger/quality.py` gives each run a score from 0 to 100 and a grade (A from 85, B from 70,
C from 55, D from 40, F below 40). It is deterministic and calls no model. The score starts at 70
and each signal that can be measured adds or subtracts points. The total is clamped to 0 to 100.
The receipt lists each signal with its value, its points and a note saying what was measured.

A signal is listed only when its input exists. A run with no test command has no test outcome, a
run with no prompts has no scope signal, and a run with no steps has no step-based signal.

| Signal | Points | What it measures |
| --- | --- | --- |
| `tests_run` | +10, or -10 | A test command ran. -10 when files changed and no test command ran. |
| `test_outcome` | +15, or -15 | The last test run with a readable result passed or failed. |
| `failing_then_passing` | +5 | A failing run was followed by a passing run that ends green. |
| `failing_streak` | -5 per repeat, max -15 | Consecutive failing test runs beyond the first. |
| `test_tampering` | -10 per test deleted or skipped, -5 per test weakened, max -25 | Test changes flagged by the risk rules. Low-severity fixture deletions do not count. |
| `error_rate` | +5 down to -15 | Share of steps that returned an error. 0% gives +5, and 100% gives -15 at most. |
| `retry_loops` | -5 each, max -15 | A non-test command repeated 3+ times, or a file edited 5+ times. |
| `scope` | -1 per unrelated file past the fourth, max -10 | Changed files that share no word with the prompts. Mild, and only when at least 5 files are unrelated and they are over half of the changes. |
| `unfinished` | -10 | The final message says it could not finish, is unable to do something, has a TODO, is not implemented, or is incomplete. |
| `cost_efficiency` | +5, 0, -5 or -10 | Estimated USD per changed line: up to $0.02, $0.10, $0.25, more. |
| `risk_level` | -20 High, -8 Medium, 0 Low | The rule-based risk level from `risk.py`. |

Test commands are recognised by name: pytest, unittest, jest, vitest, mocha, `go test`,
`cargo test`, and `npm`, `pnpm`, `yarn` or `bun` test. Results are read from their summary lines.
A failed command with no summary counts as a failure. A successful command with output but no
summary counts as a pass.

## Cost recommendations

`runledger/advisor.py` lists where a run spent more than it needed to. Each entry has a `kind`, a
`title`, a `detail`, an `est_savings_usd` and the `steps` it concerns. The list is sorted by
estimated saving, largest first. Entries with no estimate come last.

Every figure is an estimate from the list prices in `prices.json`, applied to the tokens each step
was charged for. Nothing here is an exact saving. Steps whose model has no price are left out of the
estimates, so the savings are lower bounds.

| Kind | When it appears | How the saving is estimated |
| --- | --- | --- |
| `model_downgrade` | Reads, searches, listings and simple read-only shell commands ran on Opus or Sonnet. | Their cost, minus the same tokens at Haiku 5.5 prices (`claude-haiku-5-5`). |
| `cache_misses` | A session of 20 or more steps with at least 50,000 input tokens, of which under 30% came from the prompt cache. | Half of the uncached input is assumed to repeat between turns. That share is priced at the input rate minus the cache-read rate. |
| `retry_loop` | A shell command failed two or more times. | The cost of every failing repeat after the first. |
| `large_reads` | A file was read three or more times, or a Read result was 4,000 characters or more. | For repeats, the cost of the reads after the second. For large results, the context they add to later steps at cache-read rates. |
| `unknown_pricing` | A model that used tokens has no list price. | No estimate (`null`). Add a price with `RUNLEDGER_PRICES`. |

A saving is kept only when it is material: at least $0.01, or at least 20% of the run's estimated
cost. A run that costs less than one cent gets no priced recommendation. Each entry is shaped as
`{"kind", "title", "detail", "est_savings_usd", "steps"}`.

## AI risk review

The AI risk review is an optional explanation layer on top of the rule-based risks. It is
**opt-in**: nothing is sent to Anthropic unless you pass `--review` to `runledger receipt` or
`runledger push`. It needs `ANTHROPIC_API_KEY`. Without the flag, the receipt is built entirely on
your machine. `--ai` summaries are a separate opt-in.

- **What it does.** The rules still decide the score, level and risk list; the model never changes
  them. It labels each finding `confirmed`, `false_positive` or `uncertain`, gives a verdict
  (`looks_safe`, `needs_review`, `dangerous`), writes a summary of up to 600 characters, and explains
  each changed file.
- **When it runs.** With no rule-based risks and no changed files there is nothing to review, so no
  request is made. Otherwise it is one request to `claude-sonnet-5-5` (`--review-model` changes it).
  Each finding gets exactly one assessment; findings the model omits are recorded as `uncertain`.
- **Escalation.** If the verdict is `dangerous` and `RUNLEDGER_ESCALATE_OPUS=1`, the request is re-run
  on `claude-opus-5-5` and that result is kept. Costs and tokens of both calls are added.
- **Failures.** The receipt is still written without the review, and one line on stderr says why.
  `push` uploads the run without the review.

**Sent to Anthropic, after redaction:**

- the first 5 user requests, each cut to 500 characters;
- up to 60 steps: step number, tool name, failure flag, relative file path, command or search/URL
  text (300 characters), edit old and new text (800 characters each, first 5 edits per step), and
  the first 800 characters of written content;
- the count of steps left out;
- the rule-based risks: step, code, severity, and reason (300 characters);
- the changed files: relative path, lines added and removed, created or deleted (up to 200 files).

**Not sent:** tool outputs, the agent's final message, the absolute working folder, the session id,
and the developer and project names used by `push`.

**Redaction** uses the same patterns as the risk rules: Anthropic, OpenAI, AWS and GitHub keys, Slack
tokens, private key blocks, bearer tokens, and quoted `key=`, `token=`, `secret=` or `password=`
values. Matches become `[REDACTED]`. Redaction is pattern-based and will miss some secrets.

**Destination:** `https://api.anthropic.com`, or `ANTHROPIC_BASE_URL` if set. No other service
receives it.

**Cost:** `cost_usd` comes from the reply usage and `prices.json`, and is `null` for unpriced models.
`tokens.input` counts cache tokens too.

**Stored result:** `ai_review` on the run, with keys `model`, `verdict`, `summary`,
`risk_assessments`, `diff_explanations`, `cost_usd` and `tokens`.
