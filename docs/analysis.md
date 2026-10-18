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
| `test_outcome` | +15, or -15 | The last test run with a readable summary passed or failed. Output with no summary is unknown: it earns no test points either way. |
| `failing_then_passing` | +5 | A failing run was followed by a passing run that ends green. |
| `failing_streak` | -5 per repeat, max -15 | Consecutive failing test runs beyond the first. |
| `test_tampering` | -10 per test deleted or skipped, -5 per test weakened, max -25 | Test changes flagged by the risk rules. Low-severity fixture deletions do not count. |
| `test_changes` | 0 | Neutral note: a test was replaced by one that covers the same behaviour, or assertions changed with no net loss. Read from the Git diff of each changed test file. |

For test files in a Git working tree, the diff against HEAD decides the verdict, however the file
was written (edit tools, heredocs, scripts): a skip marker added is skipped; tests removed and new
ones added that share a name word with them (or with the request), with no fewer assertions, is
replaced; fewer assertions, or a removed test with no replacement, is weakened. When a file has a
diff verdict, it replaces what the per-step Edit rule said about that file. When HEAD is newer
than the session start (the changes were committed before the receipt ran), the diff is taken
against the last commit before the session start instead, recorded as `git_base` in JSON.
The diff is read once per receipt and shared with the risk rules.
| `error_rate` | +5 down to -15 | Share of steps that returned an error. 0% gives +5, and 100% gives -15 at most. |
| `retry_loops` | -5 each, max -15 | A non-test command repeated 3+ times, or a file edited 5+ times through an edit tool or a shell command (`echo x >> a.py`, `sed -i`, `Set-Content`). |
| `scope` | -1 per unrelated file past the fourth, max -10 | Changed files that share no word with the prompts. Mild, and only when at least 5 files are unrelated and they are over half of the changes. |
| `unfinished` | -10 | The final message says it could not finish, is unable to do something, has a TODO, is not implemented, or is incomplete. |
| `cost_efficiency` | +5, 0, -5 or -10 | Estimated USD per changed line: up to $0.02, $0.10, $0.25, more. |
| `risk_level` | -20 High, -8 Medium, 0 Low | The rule-based risk level from `risk.py`. |

A test command is a command that runs a runner: each `&&`, `;`, `|` or newline segment is judged by
the command word it starts with, after `FOO=1` assignments, wrappers (`uv run`, `poetry run`,
`npx`, `python -m`) and PowerShell's call operator (`& pytest -q`,
`& "C:\Python312\python.exe" -m pytest -q`) are stripped. Runners are pytest, `py.test`, unittest,
tox, nox, jest, vitest, mocha, rspec, `go test`, `cargo test`, `dotnet test`, `mvn test`,
`gradle test`, `make test` and a
`npm`/`pnpm`/`yarn`/`bun` `test` script. Installing (`pip install pytest`, `npm install jest`),
inspecting (`pytest --version`, `--help`, `--collect-only`, `jest --list-tests`) and merely
mentioning a runner (`grep pytest`, `echo npm test`) is not a run. An install verb (`install`,
`uninstall`, `add`, `i`, `remove`) only counts as an install when it is a package manager's
subcommand (`pip install pytest`, `uv add pytest`, `npm i jest`), so a run whose arguments contain
one of those words (`pytest -k add`, `npm test -- --grep install`) still counts. Results are read
from the runner's summary lines: the pytest footer or quiet form (counts and a timing and nothing
else, as in `3 passed in 0.20s` or `=== 2 failed, 3 passed in 0.50s ===`), the jest/vitest `Tests:`
line, mocha's `N passing`/`N failing`, go's `ok`/`FAIL` lines, cargo's `test result:` line and
unittest's `Ran N tests` footer. A count in free-form text (`retried after 3 errors in 0.10s`) is
not a summary, and `0 errors` is not a failure. An explicit `N failed` still marks the run failed
when there is no summary line, as in v0.6.0. A failed command with no summary counts as a failure;
a successful command whose output has no readable summary is unknown, so it adds no test points.

## Cost recommendations

`runledger/advisor.py` lists where a run spent more than it needed to. Each entry has a `kind`, a
`title`, a `detail`, an `est_savings_usd` and the `steps` it concerns. The list is sorted by
estimated saving, largest first. Entries with no estimate come last.

Every figure is an estimate from the list prices in `prices.json`, applied to the tokens each step
was charged for. Nothing here is an exact saving. Steps whose model has no price are left out of the
estimates, so the savings are lower bounds.

| Kind | When it appears | How the saving is estimated |
| --- | --- | --- |
| `model_downgrade` | Reads, searches, listings and simple read-only shell commands ran on Opus or Sonnet. | Their cost, minus the same tokens at Haiku 5.5 prices (`claude-haiku-5-5`). A price table without a Haiku price gives no downgrade advice at all. |
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
  on `claude-opus-5-5` and that result is kept (`escalated: true`). Costs and tokens of both calls are
  added. If the second call fails, the first review is kept, `escalated` stays `false` and `notes`
  records one line starting with `escalation failed:`.
- **Failures.** The receipt is still written without the review, and one line on stderr says why.
  `push` uploads the run without the review.

**Sent to Anthropic, after masking:**

- the first 5 user requests, each cut to 500 characters;
- up to 60 steps: step number, tool name, failure flag, relative file path, command or search/URL
  text (300 characters), edit old and new text (800 characters each, first 5 edits per step), and
  the first 800 characters of written content;
- the count of steps left out;
- the rule-based risks: step, code, severity, and reason (300 characters);
- the changed files: relative path, lines added and removed, created or deleted (up to 200 files).

**Not sent:** tool outputs, the agent's final message, the absolute working folder, the session id,
and the developer and project names used by `push`.

**Masking** happens before anything is sent, with no opt-out:

- **Secrets** use the same patterns as the risk rules: Anthropic, OpenAI, AWS and GitHub keys, Slack
  tokens, private key blocks, bearer tokens, and quoted `key=`, `token=`, `secret=` or `password=`
  values. Matches become `[REDACTED]`.
- **E-mail addresses** become `[EMAIL]`.
- **Home folders** become `~`: the user name of `C:\Users\<name>`, `/Users/<name>` and `/home/<name>`
  never leaves the machine, and the rest of the path is kept (`C:\Users\ana\app\x.py` is sent as
  `~\app\x.py`).

`--ai` summaries are masked the same way: the prompts, each step's inputs, each tool result and the
agent's final message all pass through it. Masking is pattern-based and will miss an unusual secret
or a path outside a home folder. The data block of a review is also escaped, so a command containing
`</run_data>` cannot close it: the text arrives as `\u003c/run_data>` and is still readable.

**Destination:** `https://api.anthropic.com`, or `ANTHROPIC_BASE_URL` if set. No other service
receives it.

**Cost:** `cost_usd` comes from the reply usage and `prices.json`, and is `null` for unpriced models.
`tokens.input` counts cache tokens too.

**Stored result:** `ai_review` on the run, with keys `model`, `verdict`, `summary`,
`risk_assessments`, `diff_explanations`, `cost_usd`, `tokens`, `escalated` and `notes`.

## Shell and PowerShell analysis

A command is read the way the shell reads it, without running it: `runledger/shell_syntax.py`
finds POSIX heredocs, `runledger/shell_writes.py` reads POSIX writes and deletes, and
`runledger/ps_writes.py` reads PowerShell ones. The files they report are the files the receipt
lists and the paths the risk rules check.

**Heredocs.** A heredoc body is data, so the commands after it are analysed and the body is not.
The scanner is quote- and comment-aware: `<<` inside `'...'` or `"..."`, in a `#` comment
(`# <<EOF`), in `$((...))` arithmetic (`echo $((1<<2))`) or as the `<<<` here-string operator
does not start a heredoc. Inside an ANSI-C string (`$'...'`) a backslash escapes the quote
(`$'it\'s <<EOF'` is one word) and a literal newline is part of the string, so a queued heredoc
takes its body from the newline that really ends the command line: in `echo <<EOF $'first` /
`second'` / `payload` / `EOF` / `rm -rf ./build`, the body is `payload` and the `rm -rf` after it
is analysed again. A `#` comment ends at the newline, so the line after it is a command of its own
(`echo ok # <<EOF` followed by `rm k.txt` still lists `k.txt` as deleted).

**When a step is read as PowerShell.** A `PowerShell` tool step always is. A `Bash` step is read
as PowerShell only when a cmdlet or a `[System.IO.File]` call is the command word of a statement,
after statement separators (`;`, `|`, newline, parentheses), prefixes such as `sudo`, `env`,
`time` and `xargs`, and `VAR=` assignments. Quoted strings, `$'...'` strings, `@'...'@`
here-strings, `<# ... #>` block comments and `#` comments are blanked first, so
`echo 'Set-Content is a cmdlet'; touch t.txt; rm r.txt` stays POSIX and keeps its writes and
deletes.

**PowerShell writes and deletes.** `Set-Content`/`sc`, `Add-Content`/`ac`, `Out-File`,
`Tee-Object`/`tee`, `Export-Csv`/`epcsv`, `New-Item`/`ni` (files, not `-ItemType Directory`),
`Copy-Item`/`cp`, `Move-Item`/`mv`, `Rename-Item`/`ren`, `Remove-Item`/`ri`/`rm`/`del`/`rd`,
`Invoke-WebRequest`/`iwr` and `Invoke-RestMethod`/`irm` with `-OutFile`, the `>`/`>>`/`n>`
redirections, and `[System.IO.File]` write calls (`WriteAllText`, `WriteAllLines`,
`AppendAllText`, `AppendAllLines`, `WriteAllBytes`). Named and positional
`-Path`/`-LiteralPath`/`-Destination`/`-NewName`/`-Name` arguments are read, an earlier
`Set-Location`/`cd` resolves later relative targets, and `Move-Item` and `Rename-Item` count as a
delete of the old path plus a write of the new one.

**What is skipped.** Targets that are not one literal file: `$variable` and `$(...)` targets,
wildcards (`*.log`, `?`), backtick interpolation, paths that end in a separator (a directory),
device names (`nul`, `con`, `com1`), non-FileSystem provider paths (`HKLM:\Software\...`,
`Cert:\...`, `Env:\...`), which change registry keys, certificates or environment variables
rather than files, and `-WhatIf`, which skips the whole statement. A `Copy-Item`, `Move-Item` or
`Rename-Item` whose source has a variable or a wildcard is skipped entirely, because the
destination cannot be known.
