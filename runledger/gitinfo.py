"""Read-only facts from the run's Git working tree: the branch, the line
counts of uncommitted changes and the diffs of chosen files. Every helper returns nothing when the folder
is missing, is not a repository, or git is not installed."""
from __future__ import annotations

import os
import subprocess
from typing import Dict, Iterable, List, Optional

# The session log names the folder, so its repository config is not trusted:
# these options keep git from running fsmonitor hooks, external diff tools or textconv filters.
_SAFE = ["-c", "core.fsmonitor=false", "-c", "core.quotepath=false"]
_MAX_COUNT_BYTES = 2 * 1024 * 1024
_MAX_DIFF_CHARS = 512 * 1024  # per file
_MAX_DIFF_PATHS = 200


def _git(cwd: str, *args: str) -> Optional[str]:
    try:
        result = subprocess.run(["git", "-C", cwd, *_SAFE, *args], capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=15,
                                stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def _in_repo(cwd: Optional[str]) -> bool:
    return bool(cwd) and os.path.isdir(cwd) and (_git(cwd, "rev-parse", "--is-inside-work-tree") or "").strip() == "true"


def current_branch(cwd: Optional[str]) -> Optional[str]:
    """The checked-out branch, or None when HEAD is detached or there is no repository."""
    if not _in_repo(cwd):
        return None
    return (_git(cwd, "symbolic-ref", "--short", "-q", "HEAD") or "").strip() or None


def _count_lines(path: str) -> int:
    try:
        if os.path.getsize(path) > _MAX_COUNT_BYTES:
            return 0
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return 0
    if b"\0" in data:
        return 0  # binary
    return len(data.splitlines())


def working_changes(cwd: Optional[str], paths: Optional[Iterable[str]] = None) -> Dict[str, Dict[str, object]]:
    """Uncommitted changes against HEAD, keyed by path relative to cwd:
    {"added": int, "removed": int, "status": "modified" | "deleted" | "untracked"}.
    With `paths`, only those paths are reported."""
    if not _in_repo(cwd):
        return {}
    wanted = None if paths is None else set(paths)
    if wanted is not None and not wanted:
        return {}
    out: Dict[str, Dict[str, object]] = {}
    if _git(cwd, "rev-parse", "--verify", "-q", "HEAD") is not None:
        numstat = _git(cwd, "diff", "HEAD", "--numstat", "-z", "--no-renames", "--relative",
                       "--no-ext-diff", "--no-textconv") or ""
        for record in numstat.split("\0"):
            parts = record.split("\t", 2)
            if len(parts) != 3 or not parts[2]:
                continue
            added, removed, path = parts
            if wanted is not None and path not in wanted:
                continue
            exists = os.path.lexists(os.path.join(cwd, path))
            out[path] = {"added": int(added) if added.isdigit() else 0,
                         "removed": int(removed) if removed.isdigit() else 0,
                         "status": "modified" if exists else "deleted"}
        untracked = _git(cwd, "ls-files", "--others", "--exclude-standard", "-z") or ""
    else:
        # No commit yet: every file in the index or the tree is new.
        untracked = _git(cwd, "ls-files", "--cached", "--others", "--exclude-standard", "-z") or ""
    for path in untracked.split("\0"):
        if path and path not in out and (wanted is None or path in wanted):
            out[path] = {"added": _count_lines(os.path.join(cwd, path)), "removed": 0, "status": "untracked"}
    return out


def file_diffs(cwd: Optional[str], paths: Iterable[str]) -> Dict[str, str]:
    """Unified diffs (no context lines) of `paths` against HEAD, keyed by path relative
    to cwd. Files with no change, untracked files and a repository without commits
    give no entry. Each diff is cut at a size limit."""
    wanted = sorted(set(paths))[:_MAX_DIFF_PATHS]
    if not wanted or not _in_repo(cwd) or _git(cwd, "rev-parse", "--verify", "-q", "HEAD") is None:
        return {}
    text = _git(cwd, "diff", "HEAD", "-U0", "--no-color", "--no-renames", "--relative",
                "--no-ext-diff", "--no-textconv", "--", *wanted) or ""
    parts: Dict[str, List[str]] = {}
    sizes: Dict[str, int] = {}
    path: Optional[str] = None
    for line in text.splitlines(keepends=True):
        if line.startswith("diff --git "):
            path = None
        elif path is None and line.startswith(("--- a/", "+++ b/")):
            path = line[6:].rstrip("\n")
            if path not in wanted:
                path = None
            continue
        if path is not None and sizes.get(path, 0) < _MAX_DIFF_CHARS:
            parts.setdefault(path, []).append(line)
            sizes[path] = sizes.get(path, 0) + len(line)
    return {p: "".join(lines) for p, lines in parts.items()}
