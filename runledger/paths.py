"""Path helpers for session paths, which may be POSIX or Windows whatever the host OS.

It also holds the one reading of the hook command a RunLedger installer writes, which the guard
and the checkpoint installer share."""
from __future__ import annotations

import ntpath
import posixpath
import re
import shlex
import sys
from functools import lru_cache
from typing import Any, Dict, List, Optional, Sequence, Tuple

_WIN_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_WIN_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")
_LONG_DRIVE_RE = re.compile(r"^[\\/]{2}\?[\\/](?=[A-Za-z]:(?:[\\/]|$))")
_LONG_UNC_RE = re.compile(r"^[\\/]{2}\?[\\/]UNC[\\/](?=[^\\/])", re.I)
_UNC_RE = re.compile(r"^[\\/]{2}(?![?.][\\/])[^\\/]+[\\/][^\\/]")

# The shape of a path inside a shell command. `PATH_NAME` is one folder or file name and
# `PATH_REST` is the rest of a path (separators allowed); a name inside a Windows path may
# contain ':' (drive letters, alternate data streams), so the two classes differ.
PATH_NAME = r"[^\s\\/'\"`<>|;:,(){}\[\]]+"
PATH_REST = r"[^\s'\"`<>|;:,(){}\[\]]*"
PATH_CHUNK = r"[^\s'\"`<>|;,(){}\[\]]+"
# A continuation word of a spaced path is part of the folder or file name above it, so it is
# never a relative operand of its own: `rm C:/repo/a.txt ../outside.txt` is two arguments.
SPACED_NAME = r"(?![.]{1,2}[\\/]|~[\\/])" + PATH_NAME
# A name may contain spaces, and a name with several words is still one path
# (`E:\Work\Client X Corp\secret.txt`): the words before the last one carry no separator,
# the last one does.
SPACED_SUFFIX = r"(?: (?:" + SPACED_NAME + r" )*" + SPACED_NAME + r"[\\/]" + PATH_REST + r")"
# A Windows drive path containing spaces, read as one token for display: a folder name may
# contain spaces, so a receipt shows `type E:\Work\Client X\secret.txt` as one path and safe
# mode masks the whole of it. A shell splits the same text at its spaces, and that shell
# reading is what findings, file changes and guard decisions are computed from; the join is
# applied when output is rendered only (`labels.show_risks` and `labels.show_reason` are its
# two callers, both after every decision).
SPACED_DRIVE_RE = re.compile(r"[A-Za-z]:[\\/]" + PATH_CHUNK + SPACED_SUFFIX + "+")


def spaced_drive_labels(text: str) -> Dict[str, str]:
    """Every leading fragment of an unquoted spaced drive path in `text` -> the whole path.

    `E:\\Work\\Client X\\secret.txt` is two words to a shell and one display path; the first
    word is the fragment. A caller relabels the paths a finding or a file entry shows with
    these, so the folder name stays together for masking (VAL-CORE-034). Quoted text is left
    alone: `"E:\\Work\\Client X\\secret.txt"` is already one word to the shell."""
    labels: Dict[str, str] = {}
    index, length, quote = 0, len(text), ""
    while index < length:
        char = text[index]
        if quote:
            if char == quote:
                quote = ""
            elif quote == '"' and char == "\\" and index + 1 < length and text[index + 1] in '\\"':
                index += 1
            index += 1
            continue
        if char in ("'", '"'):
            quote = char
            index += 1
            continue
        m = SPACED_DRIVE_RE.match(text, index)
        if m is None:
            index += 1
            continue
        whole = m.group(0)
        head, sep, _ = whole.partition(" ")
        if sep:
            labels.setdefault(head, whole)
        index = m.end()
    return labels


@lru_cache(maxsize=64)
def _labels_re(fragments: Tuple[Tuple[str, str], ...]) -> "re.Pattern[str]":
    """One alternative per fragment, refusing an occurrence that already carries the whole path
    (the quoted form of the same text), so a label is never applied twice."""
    alts = [re.escape(fragment) + "(?!" + re.escape(whole[len(fragment):]) + ")"
            for fragment, whole in fragments]
    return re.compile("|".join(alts))


def show_spaced_drive_paths(text: str, labels: Dict[str, str]) -> str:
    """`text` with every leading fragment of a spaced drive path shown as the whole path.

    This is a display step: it rewrites the text of a reason, never the finding itself. Codes,
    severities, changed files and guard decisions are always computed from the shell's words."""
    hits = tuple(sorted(((f, w) for f, w in labels.items() if f in text),
                        key=lambda fw: len(fw[0]), reverse=True))
    if not hits:
        return text
    return _labels_re(hits).sub(lambda m: labels[m.group(0)], text)


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


# The basename of an interpreter executable a hook installer can write: it stores the literal
# `sys.executable`, so versioned, free-threaded and debug names are interpreter commands too.
_PYTHON_EXE_RE = re.compile(r"(?:python|pypy)[0-9]*(?:\.[0-9]+)*[a-z0-9_]*\Z", re.I)


def is_python_executable(name: str) -> bool:
    """True for an interpreter name such as `python3.13t.exe`, `pythonw` or `pypy3`."""
    if not name:
        return False
    stem = name[:-4] if name[-4:].lower() == ".exe" else name
    return _PYTHON_EXE_RE.fullmatch(stem) is not None


# Outside quotes these characters make a shell run a second command, redirect output, substitute
# a command or open a sub-shell. A hook entry that holds one is not the single command the
# installer writes, so it stays foreign.
_SHELL_OPERATORS = frozenset(";&|<>`\n\r()")
_SUBSTITUTION_HEADS = frozenset("({")


def has_shell_operator(command: str) -> bool:
    """True when `command` holds an unquoted shell operator, redirection, command substitution
    or sub-shell. Quoted text is data: a quoted executable path may contain any of them. A `$`
    or a backtick inside double quotes is not data, though: a POSIX shell executes those
    substitutions (`"/usr/$(printf bin)/python3.13t"` computes its own path), so the command is
    not the single simple command an installer writes."""
    quote = ""
    index, length = 0, len(command)
    while index < length:
        char = command[index]
        if quote:
            if char == quote:
                quote = ""
            elif quote == '"':
                if char == "\\" and index + 1 < length and command[index + 1] in '\\"':
                    index += 1
                elif char in "$`":
                    return True
            index += 1
            continue
        if char in ("'", '"'):
            quote = char
        elif char in _SHELL_OPERATORS:
            return True
        elif char == "$" and index + 1 < length and command[index + 1] in _SUBSTITUTION_HEADS:
            return True
        index += 1
    return False


def command_words(command: str) -> Optional[List[str]]:
    """The words of `command` when it is exactly one simple command, else None. `shlex` is used
    in its POSIX-off mode, which keeps Windows backslashes and the quotes around a word."""
    if has_shell_operator(command):
        return None
    try:
        return shlex.split(command, posix=False)
    except ValueError:
        return None


def _file_name(token: str) -> str:
    """The file name of a command word, with either kind of separator."""
    return token.replace("\\", "/").rsplit("/", 1)[-1]


def is_current_interpreter(token: str) -> bool:
    """True when the command word names the interpreter that is running (`sys.executable`). The
    installer writes that literal path, so a renamed copy of the interpreter (`agent-python.exe`)
    is a RunLedger interpreter command too. Paths are normalised, and compared case-insensitively
    when either looks like a Windows path."""
    current = sys.executable
    if not current:
        return False
    win = is_windows_style(token, current)
    return key(token, win) == key(current, win)


def is_runledger_executable(token: str) -> bool:
    """True for the `runledger` command word, with or without a path or an `.exe` suffix."""
    return _file_name(token).casefold() in ("runledger", "runledger.exe")


def owned_hook_command(command: Any, tail: Sequence[str]) -> Optional[List[str]]:
    """The words of `command` when it is exactly one RunLedger hook command ending in `tail`,
    else None. `tail` is `("guard",)` or `("checkpoint", "--hook")`, and the accepted forms are
    the two the installer writes: `<runledger>` + tail, and `<interpreter> -m runledger` + tail
    for `sys.executable` itself (whatever its file is called) or an interpreter-looking name
    (`python3.13t.exe`, `pythonw`, `pypy3`). A command with a shell operator, a redirection, a
    command substitution (`$` or a backtick, even inside double quotes), a second command or
    any other tail is a foreign hook: install must append beside it and uninstall must leave it
    alone."""
    if not isinstance(command, str):
        return None
    words = command_words(command)
    if not words:
        return None
    words = [w[1:-1] if len(w) >= 2 and w[0] in ("'", '"') and w[-1] == w[0] else w for w in words]
    if "$" in words[0] or "`" in words[0]:
        return None  # the executable word computes its own path: not the command we wrote
    suffix = list(tail)
    if words[1:] == suffix and is_runledger_executable(words[0]):
        return words
    if (words[1:3] == ["-m", "runledger"] and words[3:] == suffix
            and (is_current_interpreter(words[0]) or is_python_executable(_file_name(words[0])))):
        return words
    return None


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
