# Security Policy

## Reporting a vulnerability

Send reports to **hello@runledger.site**. Please do not open a public issue or pull request
for a security problem.

Include:

- the RunLedger version (`runledger --version`) and how you installed it (pip, the Docker
  image, or from source),
- the steps to reproduce, or a proof of concept,
- what an attacker could do, and what they need first (for example network access, or a
  team key).

Give us a reasonable time to fix the problem before you disclose it publicly. We will
acknowledge your report, keep you informed while we work on it, and credit you in the
changelog if you want that.

## Supported versions

| Version | Security fixes |
| --- | --- |
| 0.4.x | Yes |
| 0.3.x | Yes, until 0.5.0 |
| 0.2.x | No; upgrade to 0.4.0. Since 0.3.0, receipts mask secrets |
| Earlier versions | No |

## Scope

In scope: the `runledger` package (the CLI, the team server, the dashboard, the approval API
and the guard hook), the Docker image and the files in `deploy/`, and the GitHub action.

Out of scope: the agents RunLedger reads (Claude Code, Codex CLI, Aider) and third-party
services.

## Known limits

Read the security checklist and the "Known limits" section of
[docs/self-hosting.md](docs/self-hosting.md) before you run the team server. In short:

- The guard is a policy check, not a sandbox. It fails open on internal errors unless
  `fail_closed` is set in either settings file.
- API keys can be rotated and revoked, but they do not expire. A leaked key stays valid until it is
  rotated or revoked.
- A receipt holds the prompts (the first three in full), file paths, the first line of each command
  (up to 120 characters), search patterns, URLs, test counts, models, cost and risk reasons. It does not
  hold file contents or command output, but the agent's own session file, which RunLedger reads and does
  not send, can. Secrets in receipts are masked by pattern, so a secret in an unknown format typed on
  a command line can still appear in one. `receipt --ai` sends session content to Anthropic.
- The guard's log and approval requests use the same pattern-based masking, described in
  [docs/guard.md](docs/guard.md#the-guard-log).
- Behind a reverse proxy, configure the proxy as a trusted proxy (`--trusted-proxy`). Until then, the
  failed-sign-in limit and the audit log use the proxy's address.
