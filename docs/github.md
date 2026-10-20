# RunLedger on GitHub pull requests

RunLedger can post a risk receipt for the AI coding agent sessions behind a pull request.
The receipt is one sticky comment that is updated on every push: a header with the
highest risk (🟢 Low, 🟡 Medium, 🔴 High, with the score), a table with one row per
session (title, agent and model, steps, estimated cost, risk), and a collapsible section
per session with the Guard decisions, the test verdict, the risk reasons and the files
changed. The comment states that it was built in safe mode, with paths shortened and
e-mail addresses masked.

The comment is only ever updated in place when it is RunLedger's own comment, or a bot's.
A comment someone else wrote that happens to contain the marker is left alone, and a new
comment is created instead.

## 1. Save the sessions into the repository

RunLedger reads the session files of every agent it supports: Claude Code `.jsonl`
transcripts under `~/.claude/projects/<project-folder>/` (on Windows:
`%USERPROFILE%\.claude\projects\`, or under `$CLAUDE_CONFIG_DIR` if you set it), Codex
rollout `.jsonl` files, Factory Droid `.jsonl` transcripts, and `.runledger.json` files
written by any agent in the RunLedger format. To find the sessions for a project:

```bash
cd path/to/project
runledger list          # recent sessions for this folder, with their ids
```

Copy the session files you want reviewed into `.runledger/sessions/` in the repository.
The action scores every `.jsonl` and `.runledger.json` file in that folder, so copy only
the sessions for the work in the pull request. A Droid session needs its sibling
`<session-id>.settings.json` next to it: that file carries the token usage and cost.

Aider's `.aider.chat.history.md` is not scored directly: the action reads session files, not the
markdown history. To get an Aider run into the comment, write it in the RunLedger format
([format.md](format.md)) into the folder, or run `runledger receipt --agent aider` locally and
share that receipt.

> **Privacy.** A session transcript is a record of everything the agent saw: your
> prompts, file contents it read or wrote, command output, and anything pasted into
> the chat. It can contain secrets, such as API keys or the contents of `.env` files.
> Review a session file before you commit it, and remove the lines you do not want
> to share. Anyone who can read the repository can read the committed file. The PR
> comment itself shows each session's first request, the file paths and the risk
> reasons, with known secret formats masked, and it is visible to everyone who can
> see the pull request. The masking in the PR comment does not sanitize the committed
> session file. For a public repository, do not commit a raw transcript unless you are
> comfortable publishing the whole file; run `runledger receipt` locally instead, or
> use this action with session files only in a private repository.

## 2. Add the workflow

Copy [`examples/workflows/runledger.yml`](../examples/workflows/runledger.yml) to
`.github/workflows/runledger.yml`. The job needs `pull-requests: write` to post the
comment, and `contents: read` to check the repository out:

```yaml
name: RunLedger receipt
on:
  pull_request:
permissions:
  contents: read
  pull-requests: write
jobs:
  receipt:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: kodji7202-code/runledger@v0.7.0
        with:
          sessions-dir: .runledger/sessions   # default
          fail-on: "60"                        # default; "" never fails the job
```

| Input | Default | Meaning |
| --- | --- | --- |
| `sessions-dir` | `.runledger/sessions` | Folder of session files (`.jsonl`, `.runledger.json`) to score |
| `fail-on` | `60` | Fail the job if the highest score is at or above this (0-100); empty = never fail |
| `allow-empty` | `false` | Accept a folder with no session files: notice, nothing posted, exit 0 |
| `python-version` | `3.11` | Python version the action installs |
| `github-token` | `${{ github.token }}` | Token that posts the comment |

Outputs: `risk_score` (0-100) and `risk_level` (`Low`, `Medium` or `High`).

Exit codes of the command the action runs: `0` ok, `1` highest score at or above
`--fail-on` (and the comment was posted), `2` bad arguments, `3` runtime error (no
session files, no pull request, no token, GitHub API error). A comment that could not be
posted is always `3`, even when the risk score is above the threshold; both messages are
printed. An empty session folder is a `3` too, unless `--allow-empty` is passed. The
action fails on any non-zero code.

## 3. Try it locally

Nothing is posted with `--dry-run`, and no network is used:

```bash
python -m runledger.github comment --session tests/fixtures/sample_session.jsonl --dry-run
python -m runledger.github comment --sessions-dir .runledger/sessions --dry-run > comment.md
```

To post from your own machine, set `GITHUB_TOKEN` to a token with pull request write
access and pass the pull request explicitly:

```bash
python -m runledger.github comment --sessions-dir .runledger/sessions \
  --repo owner/name --pr 42 --fail-on 60
```

## Notes

- **Forks.** Pull requests from forks get a read-only token, so GitHub refuses the
  comment. RunLedger detects that (`Resource not accessible by integration`, or a
  `pull_request` event whose head repository is a fork), writes the full receipt to the
  job summary instead, prints a notice and exits `0` (the `fail-on` threshold still
  applies). The receipt is then on the workflow run page rather than on the pull request.
  Any other refusal (401, or a 403 with a different message) stays an error with exit `3`.
- **One bad session.** A session file that cannot be read is skipped with a
  `::warning file=…::` annotation and named in the comment ("N session(s) could not be
  read"). The other sessions are still scored and posted. If no file can be read at all,
  nothing is posted and the exit code is `3`.
- **GitHub Enterprise Server.** GitHub sets `GITHUB_API_URL` in Actions automatically.
  Outside Actions, set it to the API base, for example `https://ghe.example.com/api/v3`.
- **Token safety.** The token is read from an environment variable (`--token-env`,
  default `GITHUB_TOKEN`) and never printed. Do not pass tokens on the command line.
- **Safe mode.** The comment is always built in safe mode: paths are shown relative to the
  repository (or with `~` for the home folder) and e-mail addresses are masked. See
  [safe mode](quickstart.md#safe-mode).
- **Session text cannot link anywhere.** The titles, risk reasons and model names taken from a
  session are escaped for Markdown, and a bare URL in them (`https://…`, `http://…`, `www.…`) is
  shown with an invisible separator inside the scheme, because GitHub links such a URL on its own.
  An `@` is split with the same separator only where GitHub would read a mention (at the start of
  the text or after a character that is not a letter, digit or underscore), so `@someone` in a
  session's text is not a mention while a remote such as `git@github.com:org/repo.git` is shown
  exactly as it was written. The footer's link to `https://runledger.site` is the only live link in
  the comment.
- **Test verdict.** The `Tests:` line describes the session's *final* test command: the number of
  test commands, that run's summary and the verdict (`last run 3 passed, 0 failed · verdict:
  passed`). When the final command's output has no readable summary, the line says `last run
  unreadable (…) · verdict: unknown` and names any readable earlier run as earlier, so a final
  unverified run is never reported as a pass. A session with no test command says `no tests run`.
- **Size.** The comment is kept under 60,000 characters. Long reports are shortened
  with a notice at the end.
