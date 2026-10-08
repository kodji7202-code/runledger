"""Rule-based risk detection. Deterministic and explainable on purpose:
every point in the score comes with a reason a reviewer can check.

Per-project tuning is read from `<cwd>/.runledger.json` (see `load_policy`).
Sessions may come from Windows (drive letters, backslashes, case-insensitive
paths) or from POSIX systems; both are handled."""
from __future__ import annotations

import json
import ntpath
import os
import posixpath
import re
import shlex
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .parser import Run, Step

SEVERITY_POINTS = {"high": 30, "medium": 15, "low": 5}
POLICY_FILE = ".runledger.json"


@dataclass
class Risk:
    severity: str  # high | medium | low
    code: str
    reason: str
    step: int


SECRET_PATTERNS = [
    r"(^|/)\.env(\.[\w-]+)?$", r"(^|/)\.envrc$", r"id_(rsa|ed25519|ecdsa|dsa)(\.pub)?$",
    r"\.pem$", r"\.key$", r"\.p12$", r"\.pfx$", r"(^|/)\.npmrc$", r"(^|/)\.pypirc$",
    r"(^|/)\.netrc$", r"(^|/)\.aws/credentials$", r"(^|/)\.ssh/", r"credentials(\.json)?$",
    r"secrets?\.(ya?ml|json|toml)$", r"(^|/)\.git-credentials$", r"service[-_]account.*\.json$",
    r"(^|/)\.docker/config\.json$", r"(^|/)\.kube/config$",
]
_SECRET_RE = re.compile("|".join(SECRET_PATTERNS), re.I)
_ENV_EXAMPLE_RE = re.compile(r"\.env\.(example|sample|template|dist)$", re.I)

TEST_RE = re.compile(r"(^|/)(tests?|__tests__|spec|specs)(/|$)|(\.|_|-)(test|spec)\.[a-z]+$|(^|/)test_[^/]+\.py$", re.I)

WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
SHELL_TOOLS = ("Bash", "PowerShell")

# Shell rules shared by Bash and PowerShell steps, matched against the raw command.
# Flag rules use lookaheads limited to one command (stops at ; & | newline), so the
# flags may come in any order.
DANGEROUS_CMDS: List[Tuple[str, str, str]] = [
    (r"\brm\b(?=[^;&|\n]*\s(?:-[a-zA-Z]*[rR][a-zA-Z]*|--recursive)(?=\s|$))"
     r"(?=[^;&|\n]*\s(?:-[a-zA-Z]*f[a-zA-Z]*|--force)(?=\s|$))",
     "high", "Recursive force delete (rm -rf)"),
    (r"\bgit\s+push\b[^;&|\n]*?(?:(?<!\S)-[a-zA-Z]*f[a-zA-Z]*(?=\s|$)"
     r"|--force(?:-with-lease|-if-includes)?(?=\s|$)|(?<=\s)\+\S)",
     "high", "Force-pushed to a git remote"),
    (r"\bgit\s+reset\s+--hard\b", "medium", "Hard git reset (discards uncommitted work)"),
    (r"\bgit\s+clean\s+-[a-zA-Z]*f", "medium", "git clean (deletes untracked files)"),
    (r"\bgit\s+push\b", "medium", "Pushed to a git remote"),
    (r"\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?(ba|z)?sh\b", "high", "Piped a downloaded script into a shell"),
    (r"\b(?:ba|z|da|k)?sh\s+<\(\s*(?:curl|wget)\b",
     "high", "Ran a script fetched with curl/wget straight into a shell"),
    (r"\b(?:ba|z|da|k)?sh\s+-c\s+[\"']?\s*\$\(\s*(?:curl|wget)\b",
     "high", "Ran a script fetched with curl/wget straight into a shell"),
    (r"\beval\s+[\"']?\s*\$\(\s*(?:curl|wget)\b",
     "high", "Ran a script fetched with curl/wget straight into a shell"),
    (r"\bchmod\s+(-R\s+)?777\b", "medium", "Made files world-writable (chmod 777)"),
    (r"\b(drop\s+(table|database)|truncate\s+table)\b", "high", "Destructive SQL (DROP/TRUNCATE)"),
    (r"\bnpm\s+publish\b|\btwine\s+upload\b|\bcargo\s+publish\b", "high", "Published a package"),
    (r"\b(printenv|env)\s*($|\|)|\becho\s+\$[A-Z_]*(KEY|TOKEN|SECRET|PASSWORD)", "medium", "Printed environment variables / secrets"),
    (r"--no-verify\b", "medium", "Skipped git hooks (--no-verify)"),
    (r"\b(kubectl|terraform)\s+(apply|delete|destroy)\b", "high", "Changed live infrastructure"),
    (r"\b(npm|pnpm|yarn)\s+(i|install|add)\b|\bpip3?\s+install\b|\bbrew\s+install\b", "low", "Installed packages"),
    (r"\b(curl|wget|http)\s+https?://", "low", "Made a network request from the shell"),
]
_DANGEROUS = [(re.compile(p, re.I), sev, why) for p, sev, why in DANGEROUS_CMDS]

# PowerShell-only rules (applied in addition to DANGEROUS_CMDS for PowerShell steps).
PS_CMDS: List[Tuple[str, str, str]] = [
    (r"\b(?:remove-item|ri|del|erase|rd|rmdir)\b(?=[^;&|\n]*\s-rec\w*)(?=[^;&|\n]*\s-fo\w*)",
     "high", "Recursive force delete (Remove-Item -Recurse -Force)"),
    (r"\b(?:iwr|irm|invoke-webrequest|invoke-restmethod|curl|wget)\b[^|;\n]*\|\s*(?:iex|invoke-expression)\b",
     "high", "Piped a downloaded script into PowerShell (iex)"),
    (r"\b(?:iex|invoke-expression)\b[^;|\n]*\(\s*(?:iwr|irm|invoke-webrequest|invoke-restmethod)\b",
     "high", "Ran a downloaded script in PowerShell (iex on a web request)"),
    (r"\b(?:start-process|saps|start)\b[^;\n]*\s-verb\s+[\"']?runas\b",
     "high", "Launched a process elevated (Start-Process -Verb RunAs)"),
    (r"\bset-executionpolicy\b", "medium", "Changed the PowerShell execution policy"),
    (r"\b(?:get-childitem|gci|dir|ls)\s+(?:-path\s+)?env:"
     r"|\$\{?env:\w*(?:key|token|secret|password)\w*\b(?!\s*=(?!=))",
     "medium", "Printed environment variables / secrets"),
]
_PS_RULES = [(re.compile(p, re.I), sev, why) for p, sev, why in PS_CMDS]

# "sudo" counts only as a real command: comments and quoted echo/printf/Write-Output
# arguments are blanked first (see _strip_for_sudo).
_SUDO_RE = re.compile(r"\bsudo\b", re.I)
_COMMENT_RE = re.compile(r"(^|[\s;&|(])#[^\n]*", re.M)
_ECHO_RE = re.compile(
    r"\b(echo|printf|write-output|write-host)\b"
    r"((?:\s+-[a-zA-Z]+)*(?:\s+(?:\"(?:[^\"\\]|\\.)*\"|'[^']*'))+)",
    re.I,
)

_DELETE_CMDS = {"rm", "unlink", "remove-item", "ri", "del", "erase", "rd", "rmdir"}
_COMMAND_WORDS = _DELETE_CMDS | {"git", "cat", "less", "head", "tail"}

# Hardcoded secrets in written content. Only the kind is reported, never the value.
_CONTENT_SECRET_RULES = [
    (re.compile(r"(?<![A-Za-z0-9])sk-ant-[A-Za-z0-9_-]{20,}"), "Anthropic API key"),
    (re.compile(r"(?<![A-Za-z0-9])sk-(?:proj-)?[A-Za-z0-9]{20,}"), "OpenAI API key"),
    (re.compile(r"(?<![A-Za-z0-9])AKIA[0-9A-Z]{16}(?![A-Za-z0-9])"), "AWS access key"),
    (re.compile(r"(?<![A-Za-z0-9])gh[pousr]_[A-Za-z0-9]{36,}"), "GitHub token"),
    (re.compile(r"(?<![A-Za-z0-9])xox[baprs]-[A-Za-z0-9-]{10,}"), "Slack token"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "private key"),
]
_GENERIC_SECRET_RE = re.compile(
    r"(api[_-]?key|secret|token|password)[\"']?\s*[:=]\s*[\"']([^\"'\s]{12,})[\"']", re.I)

PATH_KEYS = ("file_path", "path", "notebook_path")
_WIN_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_WIN_ROOT_RE = re.compile(r"^(?:[A-Za-z]:)?[\\/]*$")
_POSIX_TEMP_ROOTS = ("/tmp", "/var/folders", "/private/tmp")
_APPDATA_TEMP_RE = re.compile(r"(?:^|[\\/])appdata[\\/]local[\\/]temp(?:[\\/]|$)", re.I)


def _paths_in_step(step: Step) -> List[str]:
    out = []
    for k in PATH_KEYS:
        v = step.input.get(k)
        if isinstance(v, str) and v:
            out.append(v)
    return out


def _basename(p: str) -> str:
    """Last path segment for both '/' and '\\' separators (independent of the OS)."""
    parts = re.split(r"[\\/]+", p.rstrip("\\/"))
    return parts[-1] if parts else p


def _slashes(p: str) -> str:
    return p.replace("\\", "/")


def _is_windows_style(*paths: Optional[str]) -> bool:
    return any(bool(p) and bool(_WIN_DRIVE_RE.match(p)) for p in paths)


def _key(p: str, win: bool) -> str:
    """Comparable form of a path: '/' separators, no '.' or '..' segments and,
    for Windows-style paths, lowercase (Windows names are case-insensitive)."""
    if win:
        return ntpath.normpath(p.replace("/", "\\")).replace("\\", "/").lower()
    return posixpath.normpath(p)


def _within(path: str, base: str) -> bool:
    """True if `path` equals `base` or lies under it. Both must be absolute.
    A drive letter on either side makes the comparison Windows-style."""
    win = _is_windows_style(path, base)
    p = _key(path, win)
    b = _key(base, win).rstrip("/")
    return p == b or p.startswith(b + "/")


def _in_temp(target: str) -> bool:
    if _APPDATA_TEMP_RE.search(target):
        return True
    roots = list(_POSIX_TEMP_ROOTS)
    try:
        roots.append(tempfile.gettempdir())
    except OSError:  # no usable temp directory
        pass
    # Filesystem roots (e.g. TMPDIR=/) would make everything "temp"; skip them.
    return any(_within(target, r) for r in roots if r and not _WIN_ROOT_RE.match(r))


_WIN_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")


def _shell_tokens(cmd: str, tool: str) -> List[str]:
    if tool == "PowerShell":
        # Not shlex: PowerShell does not treat backslashes as escapes (C:\x stays intact).
        out = []
        for t in re.findall(r"""'[^']*'|"[^"]*"|[^\s'";|&()]+""", cmd):
            if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'":
                t = t[1:-1]
            out.append(t)
        return out
    try:
        toks = shlex.split(cmd, posix=True)
    except ValueError:
        toks = cmd.split()
    # shlex consumes backslashes, so an unquoted C:\Users\x is also read from the raw text.
    return toks + re.findall(r"[A-Za-z]:[\\/][^\s'\";|&()]*", cmd)


def _strip_for_sudo(cmd: str) -> str:
    """Blank comments and quoted echo/printf/Write-Output arguments, so a word
    'sudo' that is only text is not reported. Deliberately simple: a '#' starts a
    comment only at the start of a word; quoted arguments are removed only right
    after those echo-style commands."""
    return _ECHO_RE.sub(r"\1", _COMMENT_RE.sub(r"\1", cmd))


def _outside(path: str, cwd: Optional[str]) -> bool:
    """True if `path` resolves outside `cwd`. Temp directories count as inside.
    Windows-style paths (drive letter on either side) compare case-insensitively
    with either separator; POSIX paths compare exactly."""
    if not cwd or not path:
        return False
    mod = ntpath if _is_windows_style(path, cwd) else posixpath
    target = path if mod.isabs(path) else mod.join(cwd, path)
    if _in_temp(target):
        return False
    return not _within(target, cwd)


def _is_secret(path: str, extra: List["re.Pattern"]) -> bool:
    q = _slashes(path)
    if _SECRET_RE.search(q) and not _ENV_EXAMPLE_RE.search(q):
        return True
    return any(rx.search(q) for rx in extra)


def _user_regexes(patterns: Any) -> List["re.Pattern"]:
    out = []
    if not isinstance(patterns, list):
        return out
    for rx in patterns:
        if not isinstance(rx, str):
            continue
        try:
            out.append(re.compile(rx, re.I))
        except re.error:
            continue  # invalid pattern in the config: skip it
    return out


def _written_texts(step: Step) -> List[str]:
    inp = step.input
    texts = [inp[k] for k in ("content", "new_string", "new_source") if isinstance(inp.get(k), str)]
    edits = inp.get("edits")
    if isinstance(edits, list):
        texts += [e["new_string"] for e in edits
                  if isinstance(e, dict) and isinstance(e.get("new_string"), str)]
    return texts


def _hardcoded_secret_kinds(text: str) -> List[str]:
    """Kinds of hardcoded secret found in `text`. Returns kind names only."""
    kinds = [kind for rx, kind in _CONTENT_SECRET_RULES if rx.search(text)]
    for m in _GENERIC_SECRET_RE.finditer(text):
        if m.group(2)[:1] in ("$", "{", "<"):  # template placeholder, not a value
            continue
        kinds.append(re.sub(r"[_-]", " ", m.group(1).lower()))
    return list(dict.fromkeys(kinds))


def _looks_like_path_arg(t: str) -> bool:
    return t.startswith(("/", "\\", "~", "..")) or bool(_WIN_ABS_RE.match(t))


def _shell_risks(step: Step, cwd: Optional[str], extra: List["re.Pattern"], risks: List[Risk]) -> None:
    cmd = str(step.input.get("command") or "")
    rules = list(_DANGEROUS)
    if step.tool == "PowerShell":
        rules += _PS_RULES
    for rx, sev, why in rules:
        if rx.search(cmd):
            risks.append(Risk(sev, "command", why, step.index))
    if _SUDO_RE.search(_strip_for_sudo(cmd)):
        risks.append(Risk("high", "command", "Ran a command with sudo", step.index))

    toks = _shell_tokens(cmd, step.tool)
    deleting = any(_basename(t).lower() in _DELETE_CMDS for t in toks) or ("git" in toks and "rm" in toks)
    for t in toks:
        if t.startswith("-") or _basename(t).lower() in _COMMAND_WORDS:
            continue
        if deleting and TEST_RE.search(_slashes(t)):
            risks.append(Risk("high", "test_deleted", f"Deleted a test file ({_basename(t)})", step.index))
        if _is_secret(t, extra):
            risks.append(Risk("high", "secret_file", f"Touched a secrets file from the shell ({_basename(t)})", step.index))
        if _looks_like_path_arg(t) and len(t) > 1 and _outside(os.path.expanduser(t), cwd):
            sev = "high" if deleting else "low"
            risks.append(Risk(sev, "shell_outside", f"Shell command referenced a path outside the working folder ({t})", step.index))


def load_policy(cwd: Optional[str]) -> Dict[str, Any]:
    """Per-project settings from `<cwd>/.runledger.json`:

        {"ignore": ["code_or_reason_substring", ...],
         "severity_overrides": {"code": "low|medium|high"},
         "extra_secret_paths": ["regex", ...]}

    `ignore` drops every risk whose code or reason contains the text
    (case-insensitive). `severity_overrides` changes the level of a risk code.
    `extra_secret_paths` are regexes added to the secret-file patterns.

    A missing, unreadable or invalid file gives the defaults; so does any invalid
    entry inside it. This function never raises."""
    policy: Dict[str, Any] = {"ignore": [], "severity_overrides": {}, "extra_secret_paths": []}
    if not cwd:
        return policy
    try:
        data = json.loads((Path(cwd) / POLICY_FILE).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError, RecursionError):
        return policy
    if not isinstance(data, dict):
        return policy

    ignore = data.get("ignore")
    if isinstance(ignore, list):
        policy["ignore"] = [s.strip() for s in ignore if isinstance(s, str) and s.strip()]

    overrides = data.get("severity_overrides")
    if isinstance(overrides, dict):
        policy["severity_overrides"] = {
            code: level.lower() for code, level in overrides.items()
            if isinstance(level, str) and level.lower() in SEVERITY_POINTS
        }

    extra = data.get("extra_secret_paths")
    if isinstance(extra, list):
        policy["extra_secret_paths"] = [rx for rx in extra if isinstance(rx, str) and rx and _compiles(rx)]
    return policy


def _compiles(rx: str) -> bool:
    try:
        re.compile(rx)
        return True
    except re.error:
        return False


def _apply_policy(risks: List[Risk], policy: Dict[str, Any]) -> List[Risk]:
    ignore = []
    for s in policy.get("ignore") or []:
        if isinstance(s, str) and s.strip():
            ignore.append(s.strip().lower())
    overrides = policy.get("severity_overrides") or {}
    kept = []
    for r in risks:
        code, reason = r.code.lower(), r.reason.lower()
        if any(s in code or s in reason for s in ignore):
            continue
        level = overrides.get(r.code)
        if isinstance(level, str) and level.lower() in SEVERITY_POINTS:
            r.severity = level.lower()
        kept.append(r)
    return kept


def assess_step(step: Step, cwd: Optional[str], policy: Optional[Dict[str, Any]] = None) -> List[Risk]:
    """Risks for one step. `policy` defaults to `load_policy(cwd)`; `assess`
    passes it in so the config file is read once per run."""
    if policy is None:
        policy = load_policy(cwd)
    extra = _user_regexes(policy.get("extra_secret_paths"))
    risks: List[Risk] = []
    tool = step.tool
    paths = _paths_in_step(step)

    for p in paths:
        if _is_secret(p, extra):
            verb = "Read" if tool == "Read" else "Modified"
            risks.append(Risk("high", "secret_file", f"{verb} a secrets file ({_basename(p)})", step.index))
        if tool in WRITE_TOOLS and _outside(p, cwd):
            risks.append(Risk("high", "write_outside", f"Wrote outside the working folder ({p})", step.index))
        elif _outside(p, cwd):
            risks.append(Risk("low", "read_outside", f"Read outside the working folder ({p})", step.index))

    if tool in ("Edit", "MultiEdit") and paths and TEST_RE.search(_slashes(paths[0])):
        edits = step.input.get("edits") or [step.input]
        removed = sum(len((e.get("old_string") or "").splitlines()) for e in edits)
        added = sum(len((e.get("new_string") or "").splitlines()) for e in edits)
        text = " ".join((e.get("old_string") or "") for e in edits)
        new_text = " ".join((e.get("new_string") or "") for e in edits)
        asserts_removed = len(re.findall(r"\b(assert|expect)\b", text)) - len(re.findall(r"\b(assert|expect)\b", new_text))
        skip_added = re.search(r"\.(skip|only)\(|@pytest\.mark\.skip|xit\(|xdescribe\(|@Ignore|@Disabled", new_text) and not re.search(r"\.(skip|only)\(|@pytest\.mark\.skip|xit\(|xdescribe\(", text)
        if skip_added:
            risks.append(Risk("high", "test_skipped", f"Disabled or skipped tests in {_basename(paths[0])}", step.index))
        elif asserts_removed >= 2 or (removed - added) >= 15:
            risks.append(Risk("medium", "test_weakened", f"Removed assertions from {_basename(paths[0])}", step.index))

    if tool in WRITE_TOOLS:
        name = _basename(paths[0]) if paths else "a file"
        for text in _written_texts(step):
            for kind in _hardcoded_secret_kinds(text):
                risks.append(Risk("high", "secret_in_content", f"Wrote a hardcoded {kind} into {name}", step.index))

    if tool in SHELL_TOOLS:
        _shell_risks(step, cwd, extra, risks)

    if tool.startswith("mcp__"):
        risks.append(Risk("low", "mcp", f"Called an external MCP tool ({tool.split('__', 2)[-1]})", step.index))

    # de-duplicate by (code, reason)
    seen = set()
    uniq = []
    for r in risks:
        key = (r.code, r.reason)
        if key not in seen:
            seen.add(key)
            uniq.append(r)
    return _apply_policy(uniq, policy)


def assess(run: Run) -> Tuple[int, str, List[Risk]]:
    policy = load_policy(run.cwd)
    all_risks: List[Risk] = []
    for s in run.steps:
        s.risks = assess_step(s, run.cwd, policy)
        all_risks.extend(s.risks)
    score = 0
    counted: Dict[Tuple[str, str], int] = {}
    for r in all_risks:
        k = (r.code, r.severity)
        counted[k] = counted.get(k, 0) + 1
        # diminishing returns for repeats of the same kind
        pts = SEVERITY_POINTS[r.severity] if counted[k] == 1 else SEVERITY_POINTS[r.severity] // 3
        score += pts
    score = min(100, score)
    level = "Low" if score < 25 else "Medium" if score < 60 else "High"
    return score, level, all_risks
