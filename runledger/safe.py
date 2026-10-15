"""Safe (shareable) receipts: no local folder layout, user names or e-mail addresses.

Path rules, applied to path fields and to every absolute path found in free text:
  - inside the repository root (Git toplevel, else the run's cwd): relative to the root
  - the home folder (~, /home/<u>, /Users/<u>, /root, C:\\Users\\<u>): `~` plus the rest
  - a prefix listed in RUNLEDGER_SAFE_PREFIXES (os.pathsep-separated): `…` plus the rest
  - temp folders (/tmp, /var/tmp, the system temp dir): kept as they are
  - a path sharing leading folders with the repository root: that prefix becomes `…`
Free text then gets the usual secret masking, e-mail addresses become [EMAIL] and the
`user:pass@` part of URLs becomes [REDACTED]@. Session id, branch and models are kept.
Every function is idempotent: applying it to its own output changes nothing.
"""
from __future__ import annotations

import dataclasses
import os
import re
import tempfile
from typing import Any, List, Optional, Sequence

from .guard import redact
from .parser import Run
from .paths import _norm, is_abs, is_windows_style, within
from .risk import Risk

ELLIPSIS = "…"
EMAIL = "[EMAIL]"
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_URL_CREDS_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9+.-]*://)[^\s/@]+@")
# An absolute path in free text: a POSIX path not glued to a word, URL, `~` or `…`,
# or a Windows drive path. It ends at whitespace, quotes or shell punctuation.
_TEXT_PATH_RE = re.compile(
    r"(?<![\w.~:/\\…-])/[^\s'\"`<>|;,(){}\[\]]+"
    r"|(?<![\w])[A-Za-z]:[\\/][^\s'\"`<>|;,(){}\[\]]*")
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
        if shared >= 2:  # at least one folder below the filesystem root or drive is shared
            return ELLIPSIS + "/" + "/".join(a[shared:]) if shared < len(a) else ELLIPSIS
    return path


def _path_in_text(match: "re.Match[str]", ctx: SafeContext) -> str:
    raw = match.group(0)
    trail = len(raw) - len(raw.rstrip(".:!?"))
    core, tail = (raw[:-trail], raw[-trail:]) if trail else (raw, "")
    if len(core) <= 1:
        return raw
    win = bool(re.match(r"^[A-Za-z]:", core))
    probe = core.replace("\\", "/") if win else core
    shown = safe_path(probe, ctx)
    return raw if shown == probe else shown + tail


def safe_text(text: Any, ctx: SafeContext) -> str:
    s = "" if text is None else str(text)
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
        r.reason = safe_text(r.reason, ctx)
    run.quality = _value(run.quality, ctx)
    run.recommendations = _value(run.recommendations, ctx)
    run.ai_review = _value(run.ai_review, ctx)
    run.guard = _value(run.guard, ctx)
    run.path = os.path.basename(run.path.replace("\\", "/")) if run.path else run.path
    run.safe = True
