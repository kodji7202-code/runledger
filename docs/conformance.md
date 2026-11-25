# Receipt conformance

RunLedger's receipt contract is only useful when every supported agent keeps producing the same
externally visible facts. The repository therefore keeps a deterministic conformance matrix in
`tests/fixtures/conformance.json`; `tools/check_conformance.py` runs it without pytest or any
third-party dependency.

```bash
python tools/check_conformance.py
python tools/check_conformance.py --json   # machine-readable report
```

The checker copies every fixture into an isolated temporary folder first. A fixture therefore
cannot accidentally pick up the branch or path of the RunLedger checkout itself. For each case it:

- builds safe and non-safe JSON receipts and validates both against the shipped
  `receipt.schema.json`;
- compares adapter id, session id where stable, cwd provenance, branch, token totals, cost,
  usage provenance, changed files, risk score/level/codes and guard counts with the manifest;
- builds Markdown and HTML receipts and checks that their file counts agree with JSON;
- checks the Markdown schema marker, safe-mode redaction and the rule that unknown token counts
  never become a fake zero cost.

The pytest suite runs the same command, so the matrix is enforced on every CI platform. The
standalone command is the release gate a source checkout can run without the development tools.

## What the committed matrix covers

| Case | Adapter/input | Main behavior frozen |
| --- | --- | --- |
| `claude` | Claude Code JSONL | token/cache totals, mixed-model cost, test deletion and secrets risk |
| `codex` | Codex CLI JSONL | apply-patch file changes, unpriced model cost, MCP risk |
| `droid` | Factory Droid JSONL + settings | settings-backed usage totals and cost |
| `droid_shell` | Factory Droid shell edits + settings | shell-written files, migration risk, copied database file risk |
| `aider` | Aider chat history | chat parsing, destructive shell command risk |
| `native` | RunLedger JSON format | the open format any agent can write |
| `guard` | Claude JSONL + guard log | guard event counts and guarded risky commands |

Fixture expectations are semantic, not screenshots of one output. They deliberately omit values
that are not stable or not part of the contract (for example Aider's path-derived session id).

## Adding or updating a case

1. Use a real or representative session when possible, but sanitize it first. Never commit
   customer data, real secrets, private repository names or identifiable user data.
2. Add the input under `tests/fixtures/`, plus any sidecar the adapter reads (for example a
   Droid `.settings.json`).
3. Add a case to `tests/fixtures/conformance.json`. Review every expected value by hand; do not
   copy a value just because the current implementation produces it.
4. Run `python tools/check_conformance.py` and the focused pytest tests for the adapter.
5. If a deliberate behavior change breaks the matrix, update the expectation in the same PR and
   explain why the new behavior is correct. A silent matrix edit is not a conformance pass.

## External real-session corpus

The committed fixtures are small enough to review. They are not a substitute for real-world
validation. `tests/test_corpus.py` can additionally run against private sessions kept outside
the repository:

```bash
# Linux/macOS: separate files with ':'. Windows PowerShell: use ';'.
RUNLEDGER_CORPUS=/path/to/session1.jsonl:/path/to/session2.jsonl pytest -q tests/test_corpus.py
```

Optional environment variables:

- `RUNLEDGER_CORPUS_EXPECT`: JSON file with expected score, level, file count, cwd provenance,
  branch and the folder to run from;
- `RUNLEDGER_CORPUS_OUT`: folder where the generated Markdown, JSON and HTML receipts are kept
  for review.

The corpus check copies each live session before reading it, verifies schema and format
consistency, checks safe-mode redaction, compares Droid totals with its settings file and makes
sure the inspected working tree is not modified. Keep that corpus outside Git.

## Limits

A passing matrix means the represented inputs keep their contract. It does not prove that a
future Claude Code, Codex CLI, Factory Droid or Aider version is unchanged. When an agent
changes its log format, add a new sanitized fixture rather than weakening the old expectations.
The schema/versioning rules in [receipt-json.md](receipt-json.md) and the producer contract in
[format.md](format.md) remain the source of truth for compatibility.
