# RunLedger on GitHub pull requests

RunLedger can post a risk receipt for the Claude Code sessions behind a pull request.
The receipt is one sticky comment that is updated on every push: a header with the
highest risk (🟢 Low, 🟡 Medium, 🔴 High, with the score), a table with one row per
session (title, model, steps, estimated cost, risk), and a collapsible section per
session with the risk reasons and the files changed.

## 1. Save the sessions into the repository

Claude Code keeps each session as a `.jsonl` file under
`~/.claude/projects/<project-folder>/` (on Windows: `%USERPROFILE%\.claude\projects\`,
or under `$CLAUDE_CONFIG_DIR` if you set it). To find the sessions for a project:

```bash
cd path/to/project
runledger list          # recent sessions for this folder, with their ids
```

Copy the session files you want reviewed into `.runledger/sessions/` in the repository.
The action scores every `.jsonl` file in that folder, so copy only the sessions for
the work in the pull request.

> **Privacy.** A session transcript is a record of everything the agent saw: your
> prompts, file contents it read or wrote, command output, and anything pasted into
> the chat. It can contain secrets, such as API keys or the contents of `.env` files.
> Review a session file before you commit it, and remove the lines you do not want
> to share. Anyone who can read the repository can read the committed file. The PR
> comment itself shows each session's first request, the file paths and the risk
> reasons, with known secret formats masked, and it is visible to everyone who can
> see the pull request.

## 2. Add the workflow

Copy [`examples/workflows/runledger.yml`](../examples/workflows/runledger.yml) to
`.github/workflows/runledger.yml` and replace `OWNER` with the account that hosts
RunLedger. The job needs `pull-requests: write` to post the comment.

```yaml
- uses: actions/checkout@v4
- uses: kodji7202-code/runledger@v0.4.0
  with:
    sessions-dir: .runledger/sessions   # default
    fail-on: "60"                        # default; "" never fails the job
```

| Input | Default | Meaning |
| --- | --- | --- |
| `sessions-dir` | `.runledger/sessions` | Folder of session `.jsonl` files to score |
| `fail-on` | `60` | Fail the job if the highest score is at or above this (0-100); empty = never fail |
| `github-token` | `${{ github.token }}` | Token that posts the comment |

Outputs: `risk_score` (0-100) and `risk_level` (`Low`, `Medium` or `High`).

Exit codes of the command the action runs: `0` ok, `1` highest score at or above
`--fail-on`, `2` bad arguments, `3` runtime error (no sessions, no pull request, no
token, GitHub API error). The action fails on any non-zero code.

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

- **Forks.** Pull requests from forks get a read-only token, so the comment cannot be
  posted and the job exits with code 3. Keep the workflow on `pull_request` for
  same-repository pull requests, or have a separate trusted job post the receipt.
- **GitHub Enterprise Server.** GitHub sets `GITHUB_API_URL` in Actions automatically.
  Outside Actions, set it to the API base, for example `https://ghe.example.com/api/v3`.
- **Token safety.** The token is read from an environment variable (`--token-env`,
  default `GITHUB_TOKEN`) and never printed. Do not pass tokens on the command line.
- **Size.** The comment is kept under 60,000 characters. Long reports are shortened
  with a notice at the end.
