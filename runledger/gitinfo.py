"""Read-only facts from the run's Git working tree: the branch and the line
counts of uncommitted changes. Every helper returns nothing when the folder
is missing, is not a repository, or git is not installed."""
from __future__ import annotations

import os
import subprocess
from typing import Dict, Iterable, Optional

# The session log names the folder, so its repository config is not trusted:
# these options keep git from running fsmonitor hooks, external diff tools or textconv filters.
_SAFE = ["-c", "core.fsmonitor=false", "-c", "core.quotepath=false"]
_MAX_COUNT_BYTES = 2 * 1024 * 1024


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
