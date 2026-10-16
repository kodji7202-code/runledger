"""Path helpers for session paths, which may be POSIX or Windows whatever the host OS."""
from __future__ import annotations

import ntpath
import posixpath
import re
from typing import Optional

_WIN_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_WIN_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")


def is_windows_style(*paths: Optional[str]) -> bool:
    return any(bool(p) and bool(_WIN_DRIVE_RE.match(p)) for p in paths)


def is_abs(p: str) -> bool:
    return bool(_WIN_ABS_RE.match(p)) or p.startswith(("/", "\\"))


def _norm(p: str, win: bool) -> str:
    """'/' separators and no '.' or '..' segments; case is kept."""
    if win:
        return ntpath.normpath(p.replace("/", "\\")).replace("\\", "/")
    return posixpath.normpath(p)


def native(p: str) -> str:
    """A Windows-style absolute path with backslashes, as Windows tools print it; Git for
    Windows reports 'C:/x'. Other paths come back unchanged."""
    return ntpath.normpath(p) if _WIN_ABS_RE.match(p) else p


def join(base: Optional[str], path: str) -> str:
    """`path` joined to `base` unless it is absolute, normalised in the style of the result:
    a Windows-style absolute result gets backslashes, any other gets '/' semantics."""
    if base and not is_abs(path):
        sep = "\\" if _WIN_ABS_RE.match(base) or base.startswith("\\") else "/"
        # Plain concatenation: ntpath.join would treat a relative 'a:b' as a drive.
        path = base.rstrip("\\/") + sep + path
    if _WIN_ABS_RE.match(path) or path.startswith("\\"):
        return ntpath.normpath(path)
    return posixpath.normpath(path)


def key(p: str, win: bool) -> str:
    """Comparable form of a path; Windows-style paths are lowercased (names are case-insensitive)."""
    return _norm(p, win).lower() if win else _norm(p, win)


def within(path: str, base: str) -> bool:
    """True if `path` equals `base` or lies under it. Both must be absolute.
    A drive letter on either side makes the comparison Windows-style."""
    win = is_windows_style(path, base)
    p = key(path, win)
    b = key(base, win).rstrip("/")
    return p == b or p.startswith(b + "/")


def relative(path: str, cwd: Optional[str]) -> str:
    """`path` relative to `cwd` with '/' separators when it lies under `cwd`, as receipts
    list files. Other paths come back unchanged, except that a relative path in a Windows
    session gets '/' separators so it matches Git's keys."""
    if not path or not cwd:
        return path
    win = is_windows_style(cwd) or is_windows_style(path)
    if not is_abs(path):
        return path.replace("\\", "/") if win else path
    if not within(path, cwd):
        return path
    p, base = _norm(path, win), _norm(cwd, win).rstrip("/")
    if len(p) <= len(base):
        return path
    return p[len(base) + 1:]
