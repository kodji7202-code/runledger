# Checkpoints and rollback

Checkpoints save your Git working tree before an agent changes it. They include tracked files,
their unstaged contents, and untracked files that Git does not ignore. They do not change HEAD,
your branch, the real index (including staged changes), or your stash.

Git must be on PATH. Checkpoints use your Git author and committer identity when available.
If an identity is missing, RunLedger supplies `RunLedger <runledger@localhost>` only in the
`commit-tree` subprocess environment, preserving any configured name or email. This also covers
hooks and rollback safety checkpoints. RunLedger never writes Git config or changes your normal
commit identity. No Python dependencies, server, account, or network access are needed.

## Save, preview, restore, undo

```bash
runledger checkpoint --label "Before refactor"
runledger checkpoints
runledger rollback CHECKPOINT_ID --dry-run
runledger rollback CHECKPOINT_ID
```

The first command prints a checkpoint id. The list shows its UTC time, label, and files that
would differ from the current working tree. The preview names files to restore or remove.
Rollback asks for confirmation in a terminal. Without a terminal it refuses unless you pass
`--yes`. A dry run never asks for confirmation or creates a checkpoint ref.

Before restoring anything, rollback saves a **safety checkpoint** of the current state. If saving
it fails, no files are restored or removed. The successful command prints an undo command:

```bash
runledger rollback SAFETY_CHECKPOINT_ID
```

Undo also creates a safety checkpoint. If restoration fails partway through, the error includes
the safety checkpoint and undo command. Stop agents and other writers before rollback. If the
working tree changes between preview and confirmation, RunLedger refuses and asks you to retry.

Other selectors and options:

```bash
runledger rollback --last                     # newest checkpoint, including a safety checkpoint
runledger rollback --run SESSION_ID           # earliest retained checkpoint for this run
runledger rollback CHECKPOINT_ID --yes        # explicit confirmation for scripts
runledger checkpoint --cwd "/path/to/project" --label "Before change"
runledger checkpoints --cwd "/path/to/project"
runledger rollback CHECKPOINT_ID --cwd "/path/to/project"
runledger checkpoints prune --keep 20 --cwd "/path/to/project"
```

Omitting a selector is the same as `--last`. `--run` chooses the **earliest** retained checkpoint,
not the last prompt, so it returns to the start of the run. Pruning removes only old RunLedger
checkpoint refs, including safety checkpoints. `--keep 0` removes all of them. It does not run
Git garbage collection, delete files, or remove ordinary branches or stash entries.

## Associate a receipt

```bash
runledger checkpoint --run SESSION_ID --label "Before agent run"
```

`--session-id` is an alias for `--run`. If neither is given, RunLedger reads `RUNLEDGER_RUN_ID`,
then `RUNLEDGER_SESSION_ID`. Use the actual session id from the agent transcript to connect a
checkpoint to a receipt. A custom id still works with `rollback --run`, but does not match a
receipt whose session id is different.

When generating a receipt, RunLedger checks the repository at the transcript's recorded `cwd`
for that session's earliest checkpoint. HTML and Markdown show a copyable
`runledger rollback CHECKPOINT_ID` command. JSON exposes:

```json
{
  "rollback": {
    "checkpoint_id": "20261015T120000000000Z-01234567",
    "command": "runledger rollback 20261015T120000000000Z-01234567"
  }
}
```

Without a matching local checkpoint, `rollback` is `null`. The command must be run in the original
repository, or with `--cwd`. A receipt does not upload checkpoint contents or make rollback work
on another clone. A saved receipt may refer to a checkpoint that was later pruned.

## Automatic Claude Code checkpoints

```bash
runledger guard install                       # project settings
runledger guard install --global              # or user settings, choose one scope
runledger guard uninstall                     # removes guard and checkpoint hooks
```

The installer preserves other settings and hooks, backs up an existing settings file, and is
idempotent. It installs unrestricted lifecycle hooks even if an existing checkpoint hook only
matches resumed sessions. Alongside the existing `PreToolUse` guard, it installs:

- `SessionStart`: `runledger checkpoint --hook`
- `UserPromptSubmit`: `runledger checkpoint --hook`

Each lifecycle hook reads `cwd` and `session_id` from the JSON event on stdin and labels the
checkpoint with the event name. It does not store the prompt in checkpoint metadata. It emits
no stdout and returns zero even outside Git or on failure, so it never blocks the agent. Outside a
Git repository it is silent; a repository Git cannot read (for example a malformed `.git/config`,
or a permission problem) counts as a failure and prints a short warning to stderr, while
`runledger checkpoint` on its own prints Git's reason. Each
invocation creates a checkpoint, including resumed sessions and unchanged trees. Prune periodically
to control retention.

## Factory Droid hooks

Droid supports the same JSON input and lifecycle events. Add the following event groups to
`.factory/hooks.json` (project) or `~/.factory/hooks.json` (user), preserving your other groups.
Replace the executable path with the absolute path to your installed `runledger`. On Windows,
use a full executable path and JSON-escaped backslashes.

```json
{
  "SessionStart": [
    {"hooks": [{"type": "command", "command": "\"/absolute/path/to/runledger\" checkpoint --hook", "timeout": 120}]}
  ],
  "UserPromptSubmit": [
    {"hooks": [{"type": "command", "command": "\"/absolute/path/to/runledger\" checkpoint --hook", "timeout": 120}]}
  ]
}
```

Standalone Droid `hooks.json` uses event names at the top level, **without** a `"hooks"` wrapper.
Use `/hooks` to review/reload the configuration. RunLedger's installer only edits Claude Code
settings; it does not install Droid hooks. See
[Factory's hooks reference](https://docs.factory.ai/harness/hooks).

## Codex and other agents

The checkpoint commands are agent-independent. For Codex, create a checkpoint before starting
the CLI or before submitting the next prompt:

```bash
runledger checkpoint --label "Before Codex"
codex
```

To integrate a lifecycle hook or wrapper supplied by your agent, call
`runledger checkpoint --run ACTUAL_SESSION_ID --cwd PROJECT_DIR` before any edits, or feed the
JSON lifecycle format above to `runledger checkpoint --hook`. No Codex settings are edited and
no particular Codex hook API is assumed. A manual checkpoint without a session id still works
by checkpoint id or `--last`, but will not appear in the session receipt automatically.

## Storage, ignored files, and limits

RunLedger copies the real index to a temporary index, sets `GIT_INDEX_FILE`, and uses `git add -A`,
`write-tree`, `commit-tree`, and `update-ref`. Commits live under
`refs/runledger/checkpoints/<UTC timestamp>-<shortid>`, not on your branch. Git's object database
and those refs are the only persistent checkpoint storage. Temporary indexes are removed.
Listing and dry-run preview may write Git blob/tree objects while comparing the current tree,
but never change your worktree, real index, or refs.

- **Ignore secrets first.** Snapshots store actual file contents, without receipt-style secret
  redaction. Tracked files are included even if an ignore rule matches them. Non-ignored secrets
  will also be captured. Do not share checkpoint refs or use `git push --mirror` without reviewing
  what they contain.
- Rollback removes subsequent files only when they are not currently Git-ignored. Ignored
  untracked files stay untouched. If an ignored file obstructs a restore, RunLedger refuses rather
  than overwrite it. Directories are removed only when empty, never recursively.
- `.git` paths and Git's internal directories cannot be restored or removed as working-tree
  files. Checkpoint storage itself necessarily writes Git objects and checkpoint refs.
- Symlinks are saved as Git symlinks where supported. Restoration never writes through a
  symlink parent. File/directory replacements are checked before restoration. Windows junctions
  and other non-symlink reparse points are refused rather than traversed, including ignored
  junctions that obstruct restoration.
- Sparse checkouts, submodules, and embedded Git repositories are refused rather than give an
  incomplete snapshot or restore. Empty directories, filesystem metadata beyond Git file modes,
  and ignored untracked contents are not saved. Git's line-ending and clean/smudge filters apply.
- The real index remains unchanged after rollback, so `git status` may still show staged changes.
  Rollback changes files, not staging, commits, branch history, external services, or databases.
- Checkpoints use local disk and are not a substitute for backups. Linked worktrees share the
  repository's checkpoint refs; select an explicit id or run id when several worktrees are active.
