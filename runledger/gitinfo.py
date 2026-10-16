"""Read-only facts from the run's Git working tree: the branch, the line
counts of changes and the diffs of chosen files, against HEAD or the session's base commit. Every helper returns nothing when the folder
is missing, is not a repository, or git is not installed."""
from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Tuple

from .paths import native

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
    # A relative cwd from a log (Droid can record ".") would resolve against RunLedger's own folder.
    return bool(cwd) and os.path.isabs(cwd) and os.path.isdir(cwd) and (_git(cwd, "rev-parse", "--is-inside-work-tree") or "").strip() == "true"


def toplevel(cwd: Optional[str]) -> Optional[str]:
    """The root folder of the work tree that contains `cwd`, or None outside Git."""
    if not _in_repo(cwd):
        return None
    top = (_git(cwd, "rev-parse", "--show-toplevel") or "").strip()
    return native(top) if top else None


def current_branch(cwd: Optional[str]) -> Optional[str]:
    """The checked-out branch, or None when HEAD is detached or there is no repository."""
    if not _in_repo(cwd):
        return None
    return (_git(cwd, "symbolic-ref", "--short", "-q", "HEAD") or "").strip() or None


def session_base(cwd: Optional[str], started: Optional[datetime]) -> Optional[str]:
    """The short sha of the last commit reachable from HEAD that was committed at or before
    `started`, when HEAD itself is newer: the run's changes were committed after it began, so
    they must be compared with that commit rather than HEAD. None when HEAD is not newer, or no
    such commit exists (no start time, no commits, a shallow clone)."""
    if started is None or not _in_repo(cwd):
        return None
    head_time = (_git(cwd, "log", "-1", "--format=%ct", "HEAD") or "").strip()
    if not head_time.isdigit():
        return None
    start = started.astimezone(timezone.utc)
    if int(head_time) <= int(start.timestamp()):
        return None
    base = (_git(cwd, "rev-list", "-1", f"--before={start:%Y-%m-%d %H:%M:%S} +0000", "HEAD") or "").strip()
    if not base:
        return None
    return (_git(cwd, "rev-parse", "--short", base) or "").strip() or None


def _count_lines(path: str) -> Tuple[int, bool]:
    """(line count, is binary) of a file; a file too large to read counts 0 lines."""
    try:
        if os.path.getsize(path) > _MAX_COUNT_BYTES:
            return 0, False
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return 0, False
    if b"\0" in data:
        return 0, True
    return len(data.splitlines()), False


_ESCAPES = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13, '"': 34, "\\": 92}


def _unquote(name: str) -> str:
    """Decode a C-quoted path from a diff header ("a/x\\ty" or octal UTF-8 bytes)."""
    if len(name) < 2 or not (name.startswith('"') and name.endswith('"')):
        return name
    body, out, i = name[1:-1], bytearray(), 0
    while i < len(body):
        c = body[i]
        if c == "\\" and i + 1 < len(body):
            octal = body[i + 1:i + 4]
            if len(octal) == 3 and all(d in "01234567" for d in octal):
                out.append(int(octal, 8) & 0xFF)
                i += 4
                continue
            nxt = body[i + 1]
            if nxt in _ESCAPES:
                out.append(_ESCAPES[nxt])
            else:
                out += nxt.encode("utf-8")
            i += 2
            continue
        out += c.encode("utf-8")
        i += 1
    return out.decode("utf-8", errors="replace")


def _header_path(line: str) -> Optional[str]:
    """The path in a `--- a/x` or `+++ b/x` line. Git adds a tab after names with spaces."""
    name = line[4:].rstrip("\n")
    if name.endswith("\t"):
        name = name[:-1]
    name = _unquote(name)
    if name.startswith(("a/", "b/")):
        return name[2:]
    return None


def working_changes(cwd: Optional[str], paths: Optional[Iterable[str]] = None,
                    base: Optional[str] = None) -> Dict[str, Dict[str, object]]:
    """Changes of the working tree against `base` (default HEAD), keyed by path relative to cwd:
    {"added": int, "removed": int, "status": "modified" | "deleted" | "added" | "untracked",
    "binary": bool}. "added" is a tracked file that `base` does not have. Binary files count 0 lines.
    With `paths`, only those paths are reported."""
    if not _in_repo(cwd):
        return {}
    wanted = None if paths is None else set(paths)
    if wanted is not None and not wanted:
        return {}
    out: Dict[str, Dict[str, object]] = {}
    rev = base or "HEAD"
    if _git(cwd, "rev-parse", "--verify", "-q", rev) is not None:
        numstat = _git(cwd, "diff", rev, "--numstat", "-z", "--no-renames", "--relative",
                       "--no-ext-diff", "--no-textconv") or ""
        new = set()
        if base:
            new = set((_git(cwd, "diff", rev, "--name-only", "-z", "--no-renames", "--relative",
                            "--diff-filter=A") or "").split("\0"))
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
                         "status": "deleted" if not exists else "added" if path in new else "modified",
                         "binary": added == "-" and removed == "-"}
        untracked = _git(cwd, "ls-files", "--others", "--exclude-standard", "-z") or ""
    else:
        # No commit yet: every file in the index or the tree is new.
        untracked = _git(cwd, "ls-files", "--cached", "--others", "--exclude-standard", "-z") or ""
    for path in untracked.split("\0"):
        if path and path not in out and (wanted is None or path in wanted):
            lines, binary = _count_lines(os.path.join(cwd, path))
            out[path] = {"added": lines, "removed": 0, "status": "untracked", "binary": binary}
    return out


def file_diffs(cwd: Optional[str], paths: Iterable[str], base: Optional[str] = None) -> Dict[str, str]:
    """Unified diffs (no context lines) of `paths` in the working tree against `base` (default
    HEAD), keyed by path relative to cwd. Files with no change, untracked files and a repository
    without commits give no entry. Each diff is cut at a size limit."""
    wanted = sorted(set(paths))[:_MAX_DIFF_PATHS]
    rev = base or "HEAD"
    if not wanted or not _in_repo(cwd) or _git(cwd, "rev-parse", "--verify", "-q", rev) is None:
        return {}
    text = _git(cwd, "diff", rev, "-U0", "--no-color", "--no-renames", "--relative",
                "--no-ext-diff", "--no-textconv", "--", *wanted) or ""
    parts: Dict[str, List[str]] = {}
    sizes: Dict[str, int] = {}
    path: Optional[str] = None
    for line in text.splitlines(keepends=True):
        if line.startswith("diff --git "):
            path = None
        elif path is None and line.startswith(("--- ", "+++ ")):
            path = _header_path(line)
            if path not in wanted:
                path = None
            continue
        if path is not None and sizes.get(path, 0) < _MAX_DIFF_CHARS:
            parts.setdefault(path, []).append(line)
            sizes[path] = sizes.get(path, 0) + len(line)
    return {p: "".join(lines) for p, lines in parts.items()}
