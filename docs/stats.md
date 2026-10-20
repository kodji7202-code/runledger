# Stats: what your sessions cost and how risky they were

`runledger stats` adds up the sessions in a folder over a time window and prints one report:
sessions, steps, tokens and cost per group, the risk and quality distribution, the most common
risk codes and advisor recommendations, and the guard's decisions. It is the "how much did this
month cost me, and what did the agent do" view, for one project or for every project.

```bash
runledger stats                                   # this folder, last 30 days, grouped by agent
runledger stats --since 7d
runledger stats --since 2026-05-01 --until 2026-06-01
runledger stats --all --by model --format json
runledger stats --agent codex --by day
runledger stats --format md > comment.md
runledger stats --no-cache
runledger stats --rebuild-cache
```

| Flag | Effect |
| --- | --- |
| `--project PATH` | Report on that folder instead of the current one. |
| `--all` | Every project RunLedger can discover, not only this folder. |
| `--agent A` | Only that agent's sessions: `claude-code`, `codex`, `droid`, `aider` or `native`. |
| `--since WHEN` | Window start: `7d`, `4w`, `1m` or `YYYY-MM-DD`. Default `--since 30d`. |
| `--until WHEN` | Window end: the same forms, or a date, which covers that whole day. Default: now. |
| `--by K` | Group the rows: `agent`, `model`, `day`, `week` or `project`. Default `agent`. |
| `--top N` | How many top risk codes, recommendations and models to list. Default `--top 5`. |
| `--format F` | `table` (default), `json` or `md` (GitHub tables, for a pull request comment). |
| `--limit N` | Count only the newest N sessions in the window. Default `--limit 500`. |
| `--no-cache` | Parse every session; neither read nor write the parse cache. |
| `--rebuild-cache` | Ignore what the cache holds and parse every session again, then rewrite it. |

`--project` and `--all` are mutually exclusive, and so are `--no-cache` and `--rebuild-cache`.

## The window

`--since` and `--until` accept three forms:

- `30d`, `7d`, `4w`, `2m`: a count of days, weeks or 30-day months back from now, in local time.
- `YYYY-MM-DD`: that day's local midnight (`--until 2026-06-01` covers all of 1 June).
- `--until` also accepts the relative forms, so `--since 30d --until 7d` means "between 30 and 7
  days ago".

A session belongs to the window by when it **started**, in local time; a session with no readable
start time falls back to the file's modification time. `--by day` and `--by week` therefore bucket
in your local time zone, and `--by week` uses ISO weeks (Monday to Sunday).

The default window is the last 30 days. A session file written before the window opened cannot
hold a newer session, so those files are skipped without being parsed.

The window is resolved from the current minute, so two runs in the same minute report the same
window (and the same JSON, apart from `generated`): a cached report and an uncached one are
comparable, and a session that started seconds ago is inside the default window.

## The table report

```text
RunLedger stats - D:\proj  (last 30 days, 41 sessions, 3 agents)

agent        sessions  steps  tokens (in / out / cache)        cost
claude-code        28  1,904        2.1M / 310K / 48M      $41.20
codex               9    702         1.0M / 120K / 6M     >=$6.10  *
droid               4    233          0.4M / 61K / 2M       $2.85
total              41  2,839        3.5M / 491K / 56M    >=$50.15  *

top models   claude-opus-4-7 $38.00 | gpt-5.2-codex >=$6.10 | claude-sonnet-4-6 $6.05
risk         Low 30 | Medium 8 | High 3   avg score 21.0   top codes: command 19, mcp 11
quality      A 12 | B 18 | C 8 | D 3   most common recommendation: add_tests (9)
guard        allow 1,204 | ask 17 | deny 6 | approved 9 | errors 0   (from .runledger/guard.log)

* partial cost: 1 of 41 session(s) is not fully priced (gpt-5.2-codex); amounts marked >= are lower bounds, n/a means no price at all.
```

The heading names the folder (or "any project" with `--all`), the window, the number of sessions
counted and how many agents they came from. The table has one row per group (`--by`), sorted by
key: agents and models alphabetically, days and weeks oldest first, projects by folder path. The
`total` row closes the table.

Below the table:

- **top models** lists the priced models by cost, up to `--top`.
- **risk** counts the sessions by risk level, the average risk score, and the most common risk
  codes with how many times each one was found.
- **quality** counts the sessions by grade (A to F) and names the most common advisor
  recommendation.
- **guard** counts the decisions in the project's `.runledger/guard.log` that fall inside the
  window: `allow`, `ask`, `deny`, how many of the asks were `approved`, and how many rows are
  errors (the guard failed open). The line names the log it read; with several projects the count
  covers every log the counted sessions point at.

## Cost honesty

A figure is only printed when it can be trusted:

- A session whose tokens all have a known price contributes an exact amount.
- A session with **no token data or no priced model** prints `n/a` and makes the group and total
  figures lower bounds, shown as `>=`.
- A session where some models are priced and some are not also prints `>=`: the known part is
  summed, the unknown part is never counted as 0.

The `*` footnote lists how many sessions are not fully priced and which models have no price, so
`n/a` is never a silent zero. Add the model's rates to `~/.runledger/prices.json` (see
[agents.md](agents.md#user-prices)) and the next run prices it.

Every figure is computed exactly the way `runledger receipt` computes it for the same session,
request by request: a session of several short requests is not priced as one long request, so a
model with a long-context tier is charged that tier only for the requests that crossed its
threshold. Cached and uncached runs report the same cost.

## Grouping

| `--by` | One row per | `key` |
| --- | --- | --- |
| `agent` (default) | agent | `claude-code`, `codex`, `droid`, `aider`, `native` |
| `model` | model used by the counted sessions | the model name, `unattributed` or `(no model)` |
| `day` | local calendar day | `2026-05-14` |
| `week` | ISO week | `2026-W20` |
| `project` | session working folder | the folder path, or `(unknown)` |

Each row carries its session count, step count, token totals (input / output / cache read) and
cost. With `--by model`, a session that used several models contributes its tokens and cost to
each of their rows, priced per model the way `receipt` prices it; its session and step counts
stay in the row of the model that used most of its tokens, so every column still sums to the
`total` row. The **top models** line below the table is separate from `--by model`: it sums
tokens per model across every counted session, most expensive first.

## JSON

`--format json` writes one object on stdout, with no heading text, so it can be piped into `jq`:

```json
{
  "version": 1,
  "generated": "2026-05-14T09:30:00",
  "scope": {"project": "D:\\proj", "agent": null},
  "window": {"since": "2026-04-14T09:30:00", "until": "2026-05-14T09:30:00"},
  "totals": {"sessions": 41, "steps": 2839,
             "tokens": {"input": 3500000, "output": 491000, "cache_read": 56000000,
                        "cache_write": 0},
             "cost": 50.15, "cost_partial": true},
  "groups": [{"key": "claude-code", "sessions": 28, "steps": 1904,
              "tokens": {"input": 2100000, "output": 310000, "cache_read": 48000000,
                         "cache_write": 0},
              "cost": 41.2, "cost_partial": false}],
  "risk": {"low": 30, "medium": 8, "high": 3, "avg_score": 21.0,
           "top_codes": [["command", 19], ["mcp", 11]]},
  "quality": {"grades": {"A": 12, "B": 18}, "top_recommendations": [["add_tests", 9]]},
  "guard": {"allow": 1204, "ask": 17, "deny": 6, "approved": 9, "error": 0},
  "partial": {"sessions": 1, "unknown_models": ["gpt-5.2-codex"]},
  "skipped": [{"path": "C:\\Users\\me\\.claude\\projects\\...\\broken.jsonl",
               "reason": "not a supported session file"}]
}
```

- `version` is the report format (`1`); `generated` is when the report was made.
- `scope` echoes `--project` (or `null` with `--all`) and `--agent` (or `null`).
- `window` is the resolved start and end, as local timestamps.
- `totals` and every `groups` entry carry `sessions`, `steps`, `tokens`, `cost` and
  `cost_partial`. `cost` is `null` when nothing could be priced; `cost_partial` is `true` when
  the figure is a lower bound.
- `risk` counts sessions by level, `avg_score` is the mean risk score of the counted sessions,
  and `top_codes` is a list of `[code, findings]` pairs, most common first.
- `quality.grades` maps each grade to its session count; `top_recommendations` is a list of
  `[kind, sessions]` pairs.
- `guard` counts decisions in the window.
- `partial.sessions` is how many counted sessions are not fully priced, and
  `partial.unknown_models` names the models with no price.
- `skipped` lists the session files that could not be read, each with a one-line reason. A
  broken file is reported and never stops the run.

`--format md` prints the same numbers as GitHub tables and bullet lines, ready to paste into a
pull request comment.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | A report was printed. |
| `1` | No sessions found for the scope, or none inside the window; the reason goes to stderr. |
| `2` | Usage error: an unknown flag, an unparsable `--since`/`--until`, or `--project` with `--all`. |

A corrupt or unreadable session file does not change the exit code: it is listed in `skipped` and
on stderr, and the rest of the report is printed. A file with no bytes at all is skipped the same
way. A session that parses but holds no step (an abandoned session, say) is still counted, the way
`runledger list` shows it.

## The parse cache

`stats` is read-only and never runs Git, a network request or an AI model. Aider history files
are only looked for in the project folder itself (the search that walks up to the Git top level
would run Git), and a Droid session whose log records a relative `cwd` is recovered without a Git
lookup. To stay fast on a large history it caches one small record per session file in
`~/.runledger/cache/stats-v1.json`:

- The key is the session file's path. A record is valid while that file's size and modification
  time are unchanged, every path the parse read or looked for beside it is still in the state the
  record was built from, the record format is the current one, and the risk policy signature is
  the same. Those paths are the Droid `<id>.settings.json` sidecar and the child settings files it
  names, each folder a Claude sub-agent transcript could live in, and the transcripts themselves;
  a sidecar or transcript that was not there when the record was built, was empty, or could not be
  read drops the record as soon as it appears, is filled in or is repaired, so a warm run reports
  what `--no-cache` reports. The signature covers `~/.runledger/config.json`
  and the project's `.runledger.json`, so editing either one re-scores the sessions on the next
  run.
- A record holds tokens **per model** together with the per-request token numbers pricing needs
  (never a prompt or a command), the risk score, level and code counts, the quality grade and the
  recommendation kinds. Cost is recomputed from the current price table on every run, so a new
  price or a new `~/.runledger/prices.json` takes effect without invalidating anything.
- The cache holds no prompts, no commands and no file contents: only counts, model names, the
  session id, the working folder, the start time, and the paths, sizes and modification times of
  the files it read.
- A parse that could not track every one of those paths is not cached at all. That happens when a
  sub-agent folder tree holds more folders than the watch covers (the watch is capped for speed)
  or when a folder cannot be listed, because a transcript written into a folder nobody watched
  changes no watched source. Such a session is parsed again on every run, so a warm report still
  equals `--no-cache`; the rest of the history keeps its records.
- A missing, unreadable or corrupt cache file is ignored and rebuilt. The file is written
  atomically, and the oldest records are dropped when it grows past its bound.
- `--no-cache` parses everything and writes nothing; `--rebuild-cache` ignores what is there and
  rewrites it. `RUNLEDGER_NO_SESSION_CACHE=1` in the environment has the same effect as
  `--no-cache`.
- While a run parses more than 25 uncached sessions it prints one progress line on stderr.

Deleting the file is always safe: the next run rebuilds it.

## Related

- [agents.md](agents.md): where each agent's sessions live and how `--agent` selects them.
- [analysis.md](analysis.md): what the risk score, the quality grade and the recommendations mean.
- [guard.md](guard.md): the guard log this report counts decisions from.
- [doctor.md](doctor.md): the setup check, including how many sessions each agent has.
