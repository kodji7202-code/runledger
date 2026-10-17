"""Read-only facts from the run's Git working tree: the branch, the line
counts of changes and the diffs of chosen files, against HEAD or the session's base commit. Every helper returns nothing when the folder
is missing, is not a repository, or git is not installed."""
from __future__ import annotations

import os
import subprocess
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Dict, Iterable, Iterator, List, Optional, Tuple

from .paths import is_unc, native

# The session log names the folder, so its repository config is not trusted:
# these options keep git from running fsmonitor hooks, external diff tools or textconv filters.
_SAFE = ["-c", "core.fsmonitor=false", "-c", "core.quotepath=false"]
_STRIP_GIT_ENV = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_PREFIX",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_GLOB_PATHSPECS",
    "GIT_NOGLOB_PATHSPECS",
    "GIT_ICASE_PATHSPECS",
)
_MAX_COUNT_BYTES = 2 * 1024 * 1024
_MAX_DIFF_CHARS = 512 * 1024  # per file
_MAX_DIFF_PATHS = 200

_TOPLEVEL_CACHE: Dict[str, str] = {}
_CONTEXT_CACHE: Optional[Dict[str, Dict[str, object]]] = None


def clear_cache() -> None:
    """Clear cached repository discovery state."""
    _TOPLEVEL_CACHE.clear()
    if _CONTEXT_CACHE is not None:
        _CONTEXT_CACHE.clear()


@contextmanager
def repo_context() -> Iterator[None]:
    """Scope per-cwd git discovery caching across a single reconciliation pass."""
    global _CONTEXT_CACHE
    prev = _CONTEXT_CACHE
    _CONTEXT_CACHE = {} if prev is None else prev
    try:
        yield
    finally:
        _CONTEXT_CACHE = prev


def _git_env() -> Dict[str, str]:
    env = os.environ.copy()
    for name in _STRIP_GIT_ENV:
        env.pop(name, None)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_LITERAL_PATHSPECS"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_PAGER"] = "cat"
    return env


def _git(cwd: str, *args: str) -> Optional[str]:
    try:
        result = subprocess.run(["git", "-C", cwd, *_SAFE, *args], capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=15,
                                stdin=subprocess.DEVNULL, env=_git_env())
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def _valid_cwd(cwd: Optional[str]) -> bool:
    # A relative cwd from a log (Droid can record ".") would resolve against RunLedger's own folder.
    return bool(cwd) and not is_unc(cwd) and os.path.isabs(cwd) and os.path.isdir(cwd)


def _norm_cwd(cwd: str) -> str:
    return os.path.normcase(os.path.normpath(cwd))


def _is_unpatched() -> bool:
    return _in_repo is _ORIG_IN_REPO and _git is _ORIG_GIT


def _probe_context(cwd: str) -> Dict[str, object]:
    key = _norm_cwd(cwd)
    if _CONTEXT_CACHE is not None and key in _CONTEXT_CACHE:
        return _CONTEXT_CACHE[key]
    combined = _git(cwd, "rev-parse", "--show-toplevel", "--symbolic-full-name", "HEAD")
    if combined is not None:
        lines = [ln.rstrip("\r") for ln in combined.rstrip("\r\n").split("\n")]
        if len(lines) == 2 and lines[0]:
            top = native(lines[0])
            ref = lines[1]
            branch = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else None
            info: Dict[str, object] = {"in_repo": True, "toplevel": top, "branch": branch, "head_valid": True}
            _TOPLEVEL_CACHE[key] = top
            _TOPLEVEL_CACHE[_norm_cwd(top)] = top
            if _CONTEXT_CACHE is not None:
                _CONTEXT_CACHE[key] = info
                _CONTEXT_CACHE[_norm_cwd(top)] = info
            return info
    top_raw = (_git(cwd, "rev-parse", "--show-toplevel") or "").strip()
    if not top_raw:
        info = {"in_repo": False, "toplevel": None, "branch": None, "head_valid": False}
        if _CONTEXT_CACHE is not None:
            _CONTEXT_CACHE[key] = info
        return info
    top = native(top_raw)
    branch = (_git(cwd, "symbolic-ref", "--short", "-q", "HEAD") or "").strip() or None
    info = {"in_repo": True, "toplevel": top, "branch": branch, "head_valid": False}
    _TOPLEVEL_CACHE[key] = top
    _TOPLEVEL_CACHE[_norm_cwd(top)] = top
    if _CONTEXT_CACHE is not None:
        _CONTEXT_CACHE[key] = info
        _CONTEXT_CACHE[_norm_cwd(top)] = info
    return info


def _in_repo(cwd: Optional[str]) -> bool:
    if not _valid_cwd(cwd):
        return False
    assert cwd is not None
    if _git is _ORIG_GIT:
        key = _norm_cwd(cwd)
        if _CONTEXT_CACHE is not None:
            return bool(_probe_context(cwd)["in_repo"])
        cached_top = _TOPLEVEL_CACHE.get(key)
        if cached_top and os.path.exists(os.path.join(cached_top, ".git")):
            return True
        top_raw = (_git(cwd, "rev-parse", "--show-toplevel") or "").strip()
        if top_raw:
            top = native(top_raw)
            _TOPLEVEL_CACHE[key] = top
            _TOPLEVEL_CACHE[_norm_cwd(top)] = top
            return True
        return False
    return (_git(cwd, "rev-parse", "--is-inside-work-tree") or "").strip() == "true"


def toplevel(cwd: Optional[str]) -> Optional[str]:
    """The root folder of the work tree that contains `cwd`, or None outside Git."""
    if _is_unpatched():
        if not _valid_cwd(cwd):
            return None
        assert cwd is not None
        key = _norm_cwd(cwd)
        if _CONTEXT_CACHE is not None and key in _CONTEXT_CACHE:
            top = _CONTEXT_CACHE[key].get("toplevel")
            return str(top) if isinstance(top, str) else None
        cached_top = _TOPLEVEL_CACHE.get(key)
        if cached_top and os.path.exists(os.path.join(cached_top, ".git")):
            return cached_top
        if _CONTEXT_CACHE is not None:
            top = _probe_context(cwd).get("toplevel")
            return str(top) if isinstance(top, str) else None
        if not _in_repo(cwd):
            return None
        return _TOPLEVEL_CACHE.get(key)
    if not _in_repo(cwd):
        return None
    assert cwd is not None
    top = (_git(cwd, "rev-parse", "--show-toplevel") or "").strip()
    return native(top) if top else None


def current_branch(cwd: Optional[str]) -> Optional[str]:
    """The checked-out branch, or None when HEAD is detached or there is no repository."""
    if _is_unpatched() and _CONTEXT_CACHE is not None:
        if not _valid_cwd(cwd):
            return None
        assert cwd is not None
        info = _probe_context(cwd)
        branch = info.get("branch")
        return str(branch) if isinstance(branch, str) and branch else None
    if not _in_repo(cwd):
        return None
    assert cwd is not None
    return (_git(cwd, "symbolic-ref", "--short", "-q", "HEAD") or "").strip() or None


def session_base(cwd: Optional[str], started: Optional[datetime]) -> Optional[str]:
    """The short sha of the last commit reachable from HEAD that was committed at or before
    `started`, when HEAD itself is newer: the run's changes were committed after it began, so
    they must be compared with that commit rather than HEAD. None when HEAD is not newer, or no
    such commit exists (no start time, no commits, a shallow clone)."""
    if started is None or not _in_repo(cwd):
        return None
    assert cwd is not None
    if _is_unpatched() and _CONTEXT_CACHE is not None:
        info = _probe_context(cwd)
        if not info.get("head_valid"):
            return None
    if _git is _ORIG_GIT:
        log_out = _git(cwd, "log", "-n", "50", "--format=%ct:%h", "HEAD")
        if not log_out:
            return None
        lines = [ln.strip() for ln in log_out.splitlines() if ln.strip()]
        if not lines:
            return None
        head_ts, _, _ = lines[0].partition(":")
        if not head_ts.isdigit():
            return None
        start = started.astimezone(timezone.utc)
        start_ts = int(start.timestamp())
        if int(head_ts) <= start_ts:
            return None
        for line in lines[1:]:
            ts_str, _, sha = line.partition(":")
            if ts_str.isdigit() and int(ts_str) <= start_ts:
                return sha.strip() or None
        return (_git(cwd, "log", "-1", "--format=%h", f"--before={start:%Y-%m-%d %H:%M:%S} +0000", "HEAD") or "").strip() or None
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


_ORIG_IN_REPO = _in_repo
_ORIG_GIT = _git


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
    assert cwd is not None
    wanted = None if paths is None else set(paths)
    if wanted is not None and not wanted:
        return {}
    path_args = (["--", *sorted(p for p in wanted if p and "\0" not in p and "\n" not in p)]
                 if wanted is not None and len(wanted) <= _MAX_DIFF_PATHS else [])
    out: Dict[str, Dict[str, object]] = {}
    rev = base or "HEAD"
    if rev == "HEAD" and _is_unpatched() and _CONTEXT_CACHE is not None and not _probe_context(cwd).get("head_valid"):
        raw = None
    else:
        raw = _git(cwd, "diff", rev, "--raw", "--numstat", "-z", "--no-renames", "--relative",
                   "--no-ext-diff", "--no-textconv", *path_args)
    if raw is not None:
        new = set()
        tokens = raw.split("\0")
        i = 0
        while i < len(tokens):
            record = tokens[i]
            if record.startswith(":") and i + 1 < len(tokens):
                status_code = record.rsplit(" ", 1)[-1][:1]
                if base and status_code == "A":
                    new.add(tokens[i + 1])
                i += 2
                continue
            parts = record.split("\t", 2)
            i += 1
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
        untracked = _git(cwd, "ls-files", "--others", "--exclude-standard", "-z", *path_args) or ""
    else:
        # No commit yet: every file in the index or the tree is new.
        untracked = _git(cwd, "ls-files", "--cached", "--others", "--exclude-standard", "-z", *path_args) or ""
    for path in untracked.split("\0"):
        if path and path not in out and (wanted is None or path in wanted):
            lines, binary = _count_lines(os.path.join(cwd, path))
            out[path] = {"added": lines, "removed": 0, "status": "untracked", "binary": binary}
    return out


def file_diffs(cwd: Optional[str], paths: Iterable[str], base: Optional[str] = None) -> Dict[str, str]:
    """Unified diffs (no context lines) of `paths` in the working tree against `base` (default
    HEAD), keyed by path relative to cwd. Files with no change, untracked files and a repository
    without commits give no entry. Each diff is cut at a size limit."""
    wanted = sorted(p for p in set(paths) if p and "\0" not in p and "\n" not in p)[:_MAX_DIFF_PATHS]
    rev = base or "HEAD"
    if not wanted or not _in_repo(cwd):
        return {}
    assert cwd is not None
    if rev == "HEAD" and _is_unpatched() and _CONTEXT_CACHE is not None and not _probe_context(cwd).get("head_valid"):
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
