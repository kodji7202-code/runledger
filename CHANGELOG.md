# Changelog

All notable changes to RunLedger are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions follow
[Semantic Versioning](https://semver.org/); before 1.0, a minor version may still
change behavior, so read the entry before you upgrade.

## [Unreleased] 0.2.0

### Added

- **Team server.** `runledger serve` collects receipts pushed from every developer on a
  team (`runledger push`), stores them in SQLite, and serves a shared dashboard with
  per-developer, per-model and per-project cost and risk totals.
- **Team setup.** `runledger team create NAME` creates a team and prints its API key once.
  Teams never see each other's runs.
- **Real-time guard.** `runledger guard` is a Claude Code `PreToolUse` hook. It checks each
  tool call before it runs and answers deny, ask, or no opinion. `runledger guard install`
  adds it to a project or to every project, and `runledger guard test` shows the decision
  for one event.
- **Approvals.** A call that the guard marks as "ask" can wait for a human decision on the
  team server. Decisions are made on the dashboard or with the team key. Pending approvals
  can notify a webhook.
- **GitHub pull requests.** A composite action (`action.yml`) and `python -m runledger.github`
  post one sticky receipt comment per pull request, with the highest risk and a row per
  session. See `docs/github.md`.
- **Agent adapters.** Codex CLI (`--agent codex`), Aider (`--agent aider`) and the native
  RunLedger format for any other agent (`--agent native`, `docs/format.md`).
- **Self-hosting.** A Docker image, a Docker Compose stack with Caddy for automatic HTTPS,
  and a bare-metal guide. See `docs/self-hosting.md`.
- **Packaging.** Apache License 2.0, PyPI metadata, CI on Linux, Windows and macOS for
  Python 3.9, 3.12 and 3.13, and a release workflow that publishes tags to PyPI.

### Changed

- Risk engine hardening (`runledger/risk.py`).

### Known limits

- Enterprise authentication is in progress. Today a team has one API key with full access
  to that team. There are no per-user accounts or roles, and no key rotation or revocation.
- No built-in data retention or deletion for stored runs.

## [0.1.0]

First working prototype (MVP).

### Added

- Receipts for a Claude Code session as HTML, Markdown or JSON: files changed with lines
  added and removed, every step in plain language, the model, tokens and estimated cost per
  step, and a risk score from 0 to 100 with reasons.
- `runledger list`, `runledger receipt` (with `--open`, `--format`, `--fail-on`) and
  `--ai` for plain-language summaries from Claude Haiku.
- Public Claude API list prices in `runledger/prices.json`, with `RUNLEDGER_PRICES` to
  override them.
- No runtime dependencies; Python 3.9 and later.
