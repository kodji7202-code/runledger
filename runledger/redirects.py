"""Refusing to read or write project bookkeeping through a redirect.

A repository controls everything inside it, so `.runledger/` can be shipped as a symlink, a
Windows junction or another reparse point. Following one turns a RunLedger write - the guard's
decision log, the checkpoint hook's size-warning marker - into a write to a file outside the
repository, such as a shell startup file. Both writers share the two checks here: the path itself
is refused, and the file is opened without following its last part, so the refusal cannot be raced
between the check and the open.

Nothing here raises: a path that cannot be inspected is not a redirect, and a file that cannot be
opened simply was not opened.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Optional


def unsupported_reparse_point(path: Path, info: os.stat_result) -> bool:
    """Detect directory junctions without following them, on old and new Python versions."""
    if stat.S_ISLNK(info.st_mode):
        return False  # Git can save symlinks; callers must still avoid walking through them.
    if (getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
            or getattr(info, "st_reparse_tag", 0)):
        return True
    is_junction = getattr(os.path, "isjunction", None)  # Python 3.12+, absent on 3.9
    return bool(is_junction and is_junction(path))


def is_redirect(path: Path) -> bool:
    """Whether `path` itself is a link or another reparse point. A hook owns no part of the
    working tree, so bookkeeping must never read or write through one of those."""
    try:
        info = path.lstat()
    except OSError:
        return False
    return stat.S_ISLNK(info.st_mode) or unsupported_reparse_point(path, info)


def open_without_following(path: Path, flags: int, mode: int) -> Optional[int]:
    """`os.open` for a file a repository could redirect. The path itself is checked first, so
    `O_CREAT` never creates the file a dangling link points at; a link as the last part is
    refused by the open itself; and the descriptor comes back only when a fresh `lstat` confirms
    it is that very file, so a path swapped in between cannot be written through either. None
    means no record was written; the caller needs no error path, because a refused redirect is
    not a failure."""
    if is_redirect(path):
        return None
    try:
        fd = os.open(path, flags | getattr(os, "O_NOFOLLOW", 0), mode)
    except OSError:
        return None
    try:
        same = os.path.samestat(os.fstat(fd), path.lstat())
    except (OSError, ValueError, AttributeError):
        same = False
    if not same:
        os.close(fd)
        return None
    return fd
