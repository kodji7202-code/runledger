"""Path helpers for session paths, which may be POSIX or Windows whatever the host OS."""
from __future__ import annotations

import ntpath
import posixpath
import re
from typing import Optional

_WIN_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_WIN_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")
_LONG_DRIVE_RE = re.compile(r"^[\\/]{2}\?[\\/](?=[A-Za-z]:(?:[\\/]|$))")
_LONG_UNC_RE = re.compile(r"^[\\/]{2}\?[\\/]UNC[\\/](?=[^\\/])", re.I)
_UNC_RE = re.compile(r"^[\\/]{2}(?![?.][\\/])[^\\/]+[\\/][^\\/]")


def strip_long_prefix(p: str) -> str:
    """Normalise `\\\\?\\C:\\x` to `C:\\x` and `\\\\?\\UNC\\srv\\share\\x` to `\\\\srv\\share\\x`.
    Real device paths (`\\\\.\\...`, `\\\\?\\GLOBALROOT...`) are returned unchanged."""
    if not p:
        return p
    if _LONG_DRIVE_RE.match(p):
        return p[4:]
    if _LONG_UNC_RE.match(p):
        return "\\\\" + p[8:]
    return p


def is_unc(p: Optional[str]) -> bool:
    """True for UNC paths (`\\\\srv\\share...`, `//srv/share...`, `\\\\?\\UNC\\srv\\share...`),
    excluding device prefixes (`\\\\.\\`, `\\\\?\\`)."""
    if not p:
        return False
    q = strip_long_prefix(p)
    return bool(_UNC_RE.match(q))


def is_windows_style(*paths: Optional[str]) -> bool:
    for p in paths:
        if not p:
            continue
        q = strip_long_prefix(p)
        if _WIN_DRIVE_RE.match(q) or _UNC_RE.match(q):
            return True
    return False


def is_abs(p: str) -> bool:
    q = strip_long_prefix(p)
    return bool(_WIN_ABS_RE.match(q)) or q.startswith(("/", "\\"))


def _norm(p: str, win: bool) -> str:
    """'/' separators and no '.' or '..' segments; case is kept."""
    q = strip_long_prefix(p)
    if win:
        return ntpath.normpath(q.replace("/", "\\")).replace("\\", "/")
    return posixpath.normpath(q)


def native(p: str) -> str:
    """A Windows-style absolute path with backslashes, as Windows tools print it; Git for
    Windows reports 'C:/x'. Other paths come back unchanged."""
    q = strip_long_prefix(p)
    return ntpath.normpath(q.replace("/", "\\")) if (_WIN_ABS_RE.match(q) or _UNC_RE.match(q)) else p


def join(base: Optional[str], path: str) -> str:
    """`path` joined to `base` unless it is absolute, normalised in the style of the result:
    a Windows-style absolute result gets backslashes, any other gets '/' semantics."""
    if base:
        base = strip_long_prefix(base)
    path = strip_long_prefix(path)
    if base and not is_abs(path):
        sep = "\\" if _WIN_ABS_RE.match(base) or base.startswith("\\") or _UNC_RE.match(base) else "/"
        # Plain concatenation: ntpath.join would treat a relative 'a:b' as a drive.
        path = base.rstrip("\\/") + sep + path
    if _WIN_ABS_RE.match(path) or path.startswith("\\") or _UNC_RE.match(path):
        return ntpath.normpath(path.replace("/", "\\"))
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
