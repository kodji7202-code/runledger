"""Safe (shareable) receipts: no local folder layout, user names or e-mail addresses.

Path rules, applied to path fields and to every absolute path found in free text:
  - inside the repository root (Git toplevel, else the run's cwd): relative to the root
  - the home folder (~, /home/<u>, /Users/<u>, /root, C:\\Users\\<u>): `~` plus the rest
  - a prefix listed in RUNLEDGER_SAFE_PREFIXES (os.pathsep-separated): `…` plus the rest
  - temp folders (/tmp, /var/tmp, the system temp dir): kept as they are
  - a path sharing leading folders with the repository root: that prefix becomes `…`
  - other non-system paths with more than two segments keep their last two parts: `…/a/b`
Free text then gets the usual secret masking, e-mail addresses become [EMAIL] (except
scp-style `git@host:org/repo` remotes) and the `user:pass@` part of URLs becomes
[REDACTED]@ (except `ssh://git@host/...`). Session id, branch and models are kept.
Every function is idempotent: applying it to its own output changes nothing.
"""
from __future__ import annotations

import dataclasses
from functools import lru_cache
import os
import re
import tempfile
from typing import Any, List, Optional, Sequence

from . import labels
from .guard import redact
from .parser import Run
from .paths import (PATH_CHUNK, PATH_NAME, PATH_REST, SPACED_SUFFIX, _norm, is_abs,
                    is_windows_style, within)
from .risk import Risk

ELLIPSIS = "…"
EMAIL = "[EMAIL]"
# An scp-style remote (`git@host:org/repo`) is kept, but only with a non-empty remote path,
# so a real address followed by a prose colon (`git@host: reply`) is still masked.
_EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9._%+-])(?<!://)"
    r"(?!git@[A-Za-z0-9.-]+\.[A-Za-z]{2,}:(?!//)[\w./~-])"
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
    r"(?![A-Za-z0-9.-])"
)
_URL_CREDS_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9+.-]*://)(?!git@)[^\s/@]+@")
# More of a path separated by spaces: a name followed by a separator, then the rest of the
# folder or file name (`E:\Work\Client X\secret.txt`).
_SPACED = SPACED_SUFFIX + "*"
# The user name of a home folder may contain spaces, in any script; it runs to a separator,
# a quote or the end of the line, so no part of the name can leak. This over-redacts prose
# after an unknown user name, which is the safe direction.
_USER = PATH_NAME + r"(?:[ \t]+" + PATH_NAME + r")*"
_HOME_REST = r"(?:[\\/]" + PATH_REST + r")?"
# An absolute path in free text: a POSIX path not glued to a word, `~` or `…`, or a Windows
# drive path. `//` is not a path start, so URLs (`https://host/x`) are left alone.
_TEXT_PATH_RE = re.compile(
    r"(?<![\w.~/\\…-])/(?:[Hh]ome|[Uu]sers)/" + _USER + _HOME_REST
    + r"|(?<![\w])[A-Za-z]:[\\/][Uu]sers[\\/]" + _USER + _HOME_REST
    + r"|(?<![\w])[A-Za-z]:[\\/]" + PATH_CHUNK + _SPACED
    + r"|(?<![\w.~/\\…-])/(?!/)[^\s'\"`<>|;:,(){}\[\]]+" + _SPACED
    + r"|(?<![\w])[A-Za-z]:[\\/][^\s'\"`<>|;,(){}\[\]]*"
)
_HOME_RE = re.compile(r"^(?:/home/[^/]+|/Users/[^/]+|/root|[A-Za-z]:/Users/[^/]+)(?=/|$)", re.I)
_TEMP_PREFIXES = ("/tmp", "/var/tmp", "/private/tmp", "/private/var/folders", "/var/folders")


@dataclasses.dataclass
class SafeContext:
    root: Optional[str] = None           # repository root, or the run's cwd outside Git
    home: Optional[str] = None
    prefixes: Sequence[str] = ()


def _clean(p: str) -> str:
    return _norm(p, is_windows_style(p)).rstrip("/") or "/"


def context(cwd: Optional[str], root: Optional[str] = None) -> SafeContext:
    if root is None and cwd:
        from .gitinfo import toplevel
        root = toplevel(cwd) or cwd
    home = os.path.expanduser("~")
    extra = [p for p in os.environ.get("RUNLEDGER_SAFE_PREFIXES", "").split(os.pathsep) if p.strip()]
    return SafeContext(root=_clean(root) if root and is_abs(root) else None,
                       home=_clean(home) if home and home != "~" and is_abs(home) else None,
                       prefixes=[_clean(p) for p in extra if is_abs(p)])


def _rest(path: str, base: str) -> str:
    """`path` after `base`, starting with '/' (empty when they are equal)."""
    return path[len(base.rstrip("/")):]


def _temp_dirs() -> List[str]:
    dirs = list(_TEMP_PREFIXES)
    try:
        dirs.append(_clean(tempfile.gettempdir()))
    except Exception:  # an unusable temp setting must not stop a receipt
        pass
    return dirs


_SYSTEM_PREFIXES = (
    "/usr", "/bin", "/sbin", "/etc", "/dev", "/proc", "/sys", "/opt/homebrew", "/lib",
    "C:/Windows", "C:/Program Files", "C:/Program Files (x86)",
)


def _is_system(p: str) -> bool:
    return any(within(p, prefix) for prefix in _SYSTEM_PREFIXES)


def safe_path(path: str, ctx: SafeContext) -> str:
    """One path under the rules above. Relative paths are returned unchanged."""
    if not path or not is_abs(path):
        return path
    p = _clean(path)
    root = ctx.root
    if root and within(p, root):
        rel = _rest(p, root).lstrip("/")
        return rel or "."
    if ctx.home and within(p, ctx.home):
        return "~" + _rest(p, ctx.home)
    home = _HOME_RE.match(p)
    if home:
        return "~" + p[home.end():]
    for prefix in ctx.prefixes:
        if within(p, prefix):
            return ELLIPSIS + _rest(p, prefix)
    if any(within(p, t) for t in _temp_dirs()):
        return path
    if root:
        win = is_windows_style(p, root)
        a, b = p.split("/"), root.split("/")
        shared = 0
        while shared < min(len(a), len(b)) and (a[shared].lower() == b[shared].lower() if win else a[shared] == b[shared]):
            shared += 1
        if shared >= 2 and len(a) - shared <= 2:  # at least one folder below the filesystem root or drive is shared
            return ELLIPSIS + "/" + "/".join(a[shared:]) if shared < len(a) else ELLIPSIS
    if not _is_system(p):
        # Foreign paths (or any path when no root is known) keep only their last two parts.
        parts = [x for x in p.split("/") if x and not re.match(r"^[A-Za-z]:$", x)]
        if len(parts) > 2:
            return ELLIPSIS + "/" + "/".join(parts[-2:])
    return path


def _path_in_text(match: "re.Match[str]", ctx: SafeContext) -> str:
    raw = match.group(0)
    trail = len(raw) - len(raw.rstrip(".:!?"))
    core, tail = (raw[:-trail], raw[-trail:]) if trail else (raw, "")
    if len(core) <= 1:
        return raw
    win = bool(re.match(r"^[A-Za-z]:", core))
    probe = core.replace("\\", "/") if win else core
    if probe.endswith(ELLIPSIS) and len(probe) > 1:
        # A path cut short by a summary (`cd /long/path/to/re…`). The cut can fall inside
        # the repository root itself, so the partial path is never `within` the root and
        # would otherwise slip through untouched (e.g. a long macOS temp folder).
        shown = _truncated_path(probe[:-len(ELLIPSIS)], ctx)
        if shown is not None:
            return shown + tail
    shown = safe_path(probe, ctx)
    return raw if shown == probe else shown + tail


def _truncated_path(base: str, ctx: SafeContext) -> Optional[str]:
    """Safe form of a path prefix cut by `…`, or None when the normal rules apply."""
    root = ctx.root
    if not base or not root or not is_abs(base):
        return None
    win = is_windows_style(base, root)
    b, r = (base.lower(), root.lower()) if win else (base, root)
    if r.startswith(b):  # the cut falls inside the root (or right at its end)
        return ELLIPSIS
    shown = safe_path(base, ctx)
    return None if shown == base else shown + ELLIPSIS


@lru_cache(maxsize=64)
def _compile_spaced_re(spaced: tuple[str, ...]) -> "re.Pattern[str]":
    alts = [re.escape(p).replace("/", r"[\\/]") + r"(?:[\\/][^\s'\"`<>|;,(){}\[\]]*)?"
            for p in sorted(spaced, key=len, reverse=True)]
    flags = re.I if any(is_windows_style(p) for p in spaced) else 0
    return re.compile("|".join(alts), flags)


def _spaced_ctx_re(ctx: SafeContext) -> Optional["re.Pattern[str]"]:
    spaced = tuple(p for p in (ctx.root, ctx.home, *ctx.prefixes) if p and " " in p)
    return _compile_spaced_re(spaced) if spaced else None


def safe_text(text: Any, ctx: SafeContext) -> str:
    s = "" if text is None else str(text)
    if " " in s:
        spaced_re = _spaced_ctx_re(ctx)
        if spaced_re is not None:
            s = spaced_re.sub(lambda m: _path_in_text(m, ctx), s)
    s = _TEXT_PATH_RE.sub(lambda m: _path_in_text(m, ctx), s)
    s = redact(s)
    s = _URL_CREDS_RE.sub(r"\1[REDACTED]@", s)
    return _EMAIL_RE.sub(EMAIL, s)


def safe_cwd(cwd: Optional[str], ctx: SafeContext) -> Optional[str]:
    """The repository folder name plus the subfolder the run started in, e.g. `app/src`."""
    if not cwd:
        return cwd
    if not is_abs(cwd):
        return safe_text(cwd, ctx)
    p = _clean(cwd)
    if ctx.root and within(p, ctx.root):
        name = ctx.root.rstrip("/").rsplit("/", 1)[-1] or ctx.root
        sub = _rest(p, ctx.root).lstrip("/")
        return safe_text(f"{name}/{sub}" if sub else name, ctx)
    return safe_path(p, ctx)


def _value(value: Any, ctx: SafeContext) -> Any:
    if isinstance(value, str):
        return safe_text(value, ctx)
    if isinstance(value, list):
        return [_value(v, ctx) for v in value]
    if isinstance(value, dict):
        return {k: _value(v, ctx) for k, v in value.items()}
    return value


def make_safe(run: Run, risks: Sequence[Risk], root: Optional[str] = None) -> None:
    """Rewrite everything a receipt shows, in place. Call it last, after redact_run.
    The list of changed files is computed first and frozen, so rewriting the step
    inputs cannot change which files a receipt counts."""
    from .summarize import file_changes
    ctx = context(run.cwd, root)
    changes = file_changes(run)
    by_step = labels.step_label_map(run)
    run.frozen_changes = {}
    for path, change in changes.items():
        shown = safe_text(path, ctx) if not is_abs(path) else safe_path(path, ctx)
        run.frozen_changes[shown] = dataclasses.replace(change, path=shown)
    run.cwd = safe_cwd(run.cwd, ctx)
    run.git_changes = {safe_text(p, ctx): c for p, c in run.git_changes.items()}
    run.test_diffs = {}
    run.prompts = [safe_text(p, ctx) for p in run.prompts]
    run.overall_summary = safe_text(run.overall_summary, ctx)
    run.final_message = safe_text(run.final_message, ctx)
    for s in run.steps:
        s.summary = safe_text(s.summary, ctx)
        s.result_text = ""
        for key, val in list(s.input.items()):
            if isinstance(val, str):
                s.input[key] = safe_text(val, ctx)
    every = {id(r): r for r in list(risks) + [r for s in run.steps for r in s.risks]}
    for r in every.values():
        r.reason = safe_text(labels.show_reason(r.reason, by_step.get(r.step)), ctx)
    run.quality = _value(run.quality, ctx)
    run.recommendations = _value(run.recommendations, ctx)
    run.ai_review = _value(run.ai_review, ctx)
    run.guard = _value(run.guard, ctx)
    run.path = os.path.basename(run.path.replace("\\", "/")) if run.path else run.path
    run.safe = True
