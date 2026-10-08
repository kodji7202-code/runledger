"""Rule-based risk detection. Deterministic and explainable on purpose:
every point in the score comes with a reason a reviewer can check.

Scoring. A risk adds its severity's points (high 30, medium 15, low 5). A repeat of
the same code at the same severity adds a third of those points. Each severity has its
own total: high is uncapped, medium adds at most 45 points and low at most 10, so a long
session full of small findings cannot reach 100 on its own. The score is the sum of the
three totals, capped at 100. The level is Low below 25, Medium below 60, and High otherwise.

Shell commands (Bash and PowerShell). The command is split into simple commands at
;, &&, ||, |, &, parentheses and newlines. Heredoc and here-string bodies and comments
are ignored. The text arguments of echo, printf, Write-Output, Write-Host, git commit or
tag -m, and of python -c, node -e, perl -e, ruby -e and php -r are not read as paths,
secrets or tests. The dangerous-command rules still see the real command text. Delete
commands are checked only against their own arguments. URLs, device paths (/dev/null,
NUL, /proc/...) and Windows switches (/F, //IM, /MT:8) are not paths. When the working
folder is a Windows path, Git Bash, MSYS, Cygwin and WSL drive paths (/d/x, /mnt/d/x,
/cygdrive/d/x) are read as drive paths. Variables $TEMP, $TMP, $HOME, $USERPROFILE and
assignments earlier in the same command are expanded; any other variable makes the
argument unknown, and its path checks are skipped. A "workdir" input outside the working
folder is a medium risk, and relative path arguments are resolved against it.

Delete tool (canonical "Delete" with file_path) is checked like rm of that path.
Write, Edit, MultiEdit and Delete inside a .git folder are medium (git_internals).

Test files: real test source (test_*.py, *.test.js, *_spec.rb, ...) deleted is high.
Fixtures and data under a test folder (fixtures/, testdata/, __snapshots__/, or .json,
.txt, .md and similar) deleted is low.

Per-project tuning is read from `<cwd>/.runledger.json` (see `load_policy`).
Sessions may come from Windows (drive letters, backslashes, case-insensitive
paths) or from POSIX systems; both are handled."""
from __future__ import annotations

import json
import ntpath
import os
import posixpath
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .parser import Run, Step

SEVERITY_POINTS = {"high": 30, "medium": 15, "low": 5}
MEDIUM_POINTS_CAP = 45  # all medium findings together add at most this many points
LOW_POINTS_CAP = 10     # all low findings together add at most this many points
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
# Real test source: deleting it is high. Anything else under a test folder is data (see _is_test_fixture).
_TEST_SOURCE_RE = re.compile(r"^test_[^/]*\.py$|[._-](test|spec)\.[a-z0-9]+$", re.I)
_FIXTURE_DIRS = {"fixtures", "fixture", "testdata", "__snapshots__", "__fixtures__", "snapshots"}
_DATA_EXTS = (".json", ".jsonl", ".ndjson", ".txt", ".md", ".csv", ".tsv", ".yaml", ".yml", ".xml",
              ".html", ".snap", ".log", ".db", ".sqlite", ".sql", ".toml", ".ini")
_GIT_DIR_RE = re.compile(r"(^|/)\.git/", re.I)

WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
DELETE_TOOLS = ("Delete",)
SHELL_TOOLS = ("Bash", "PowerShell")

# Shell rules shared by Bash and PowerShell steps, matched against the command text
# with heredoc bodies, comments and text arguments removed (see _code_view).
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
    (r"(?<![\w./\\-])(?:printenv|env)\s*(?:$|\|)|\becho\s+\$[A-Z_]*(KEY|TOKEN|SECRET|PASSWORD)",
     "medium", "Printed environment variables / secrets"),
    (r"--no-verify\b", "medium", "Skipped git hooks (--no-verify)"),
    (r"\b(kubectl|terraform)\s+(apply|delete|destroy)\b", "high", "Changed live infrastructure"),
    (r"\b(npm|pnpm|yarn)\s+(i|install|add)\b|\bpip3?\s+install\b|\bbrew\s+install\b", "low", "Installed packages"),
    (r"\btaskkill\b(?=[^;&|\n]*\s//?im\b)(?![^;&|\n]*\s//?pid\b)",
     "medium", "Killed all processes by image name (taskkill /IM)"),
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

_SUDO_RE = re.compile(r"\bsudo\b", re.I)

# Commands whose arguments are text, not paths. Quoted text is blanked before the
# dangerous-command rules run (plain $VAR words stay, so printing a secret is still seen).
_TEXT_CMDS = {"echo", "printf", "write-output", "write-host"}
_INTERPRETERS = {"python", "python3", "py", "node", "perl", "ruby", "php"}
_CODE_FLAGS = {"-c", "-e", "-r", "--eval"}
_MSG_FLAG_RE = re.compile(r"^-[A-Za-z]*m$")
_DELETE_CMDS = {"rm", "unlink", "remove-item", "ri", "del", "erase", "rd", "rmdir"}
_WRAPPERS = {"sudo", "nohup", "time", "command", "builtin", "exec", "env", "nice", "xargs"}
_POSIX_ROOT_NAMES = {"tmp", "etc", "var", "usr", "bin", "sbin", "opt", "home", "mnt", "srv", "lib",
                     "lib64", "run", "sys", "boot", "root", "dev", "proc", "private", "media", "snap"}

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
_WIN_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")
_POSIX_TEMP_ROOTS = ("/tmp", "/var/folders", "/private/tmp", "/private/var/folders", "/var/tmp")
_APPDATA_TEMP_RE = re.compile(r"(?:^|[\\/])appdata[\\/]local[\\/]temp(?:[\\/]|$)", re.I)
_MSYS_DRIVE_RE = re.compile(r"^/(?:(?:mnt|cygdrive)/)?([A-Za-z])(?=/|$)", re.I)
_DEVICE_PREFIXES = ("/dev/", "/proc/", "/sys/", "//./", "//?/")
_DEVICE_NAMES = {"nul", "con", "conin$", "conout$"}
_SWITCH_RE = re.compile(r"^/{1,2}([A-Za-z?]{1,3})(?:[:+-].*)?$")
_URL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_DOTDOT_RE = re.compile(r"(^|[\\/])\.\.([\\/]|$)")
_SED_RE = re.compile(r"^[sy]([/|#,:!@%])")
_VAR_RE = re.compile(r"\$\{(\w+)\}|\$env:(\w+)|\$(\w+)", re.I)
_RESOLVABLE_ENV = ("TEMP", "TMP", "HOME", "USERPROFILE")
_ASSIGN_RE = re.compile(r"^[A-Za-z_]\w*=")
_HEREDOC_RE = re.compile(r"(?<!<)<<(?!<)(-?)[ \t]*(['\"]?)([A-Za-z_][\w.-]*)\2")
_PS_HERE_START_RE = re.compile(r"@(['\"])[ \t\r]*$")
_REDIR_RE = re.compile(r"&>>|&>|<<<|<<-|<<|>>|>&|>\||<&|<>|>|<")


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


def _is_abs(p: str) -> bool:
    return bool(_WIN_ABS_RE.match(p)) or p.startswith(("/", "\\"))


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


def _is_device(p: str) -> bool:
    """Device and special files (/dev/null, NUL, /proc/self/...) are never paths in a project."""
    q = _slashes(p).lower()
    return q.startswith(_DEVICE_PREFIXES) or _basename(p).lower() in _DEVICE_NAMES


def _msys_to_windows(p: str, win: bool) -> str:
    """/d/x, /mnt/d/x and /cygdrive/d/x become D:/x when the working folder is a Windows path."""
    if not win:
        return p
    m = _MSYS_DRIVE_RE.match(p)
    if not m:
        return p
    return m.group(1).upper() + ":" + (p[m.end():] or "/")


def _outside(path: str, cwd: Optional[str], base: Optional[str] = None) -> bool:
    """True if `path` resolves outside `cwd`. Temp directories count as inside.
    A relative path is resolved against `base` (a working directory) when given,
    otherwise against `cwd`. Windows-style paths (drive letter on either side) compare
    case-insensitively with either separator; POSIX paths compare exactly."""
    if not cwd or not path or _is_device(path):
        return False
    win = _is_windows_style(cwd)
    path = _msys_to_windows(path, win)
    if base:
        base = _msys_to_windows(base, win)
    mod = ntpath if _is_windows_style(path, cwd, base) else posixpath
    # _is_abs, not mod.isabs: ntpath.isabs("/tmp") is False on Python 3.13+
    target = path if _is_abs(path) else mod.join(base or cwd, path)
    if _in_temp(target):
        return False
    return not _within(target, cwd)


def _resolved(path: str, base: Optional[str]) -> str:
    """The path as it is shown in a reason: relative paths joined onto `base` when given."""
    if not base or _is_abs(path):
        return path
    if _is_windows_style(base):
        return ntpath.join(base, path.replace("/", "\\"))
    return posixpath.join(base, path)


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


def _is_test_fixture(path: str) -> bool:
    """True for data under a test folder (fixtures, snapshots, .json and similar).
    Real test source (test_*.py, *.test.js, *_test.go, ...) is never a fixture."""
    q = _slashes(path)
    name = q.rsplit("/", 1)[-1]
    if _TEST_SOURCE_RE.search(name):
        return False
    if any(part in _FIXTURE_DIRS for part in q.lower().split("/")):
        return True
    return name.lower().endswith(_DATA_EXTS)


def _test_deleted(path: str, index: int, risks: List[Risk]) -> None:
    """Deleting a test file is high; deleting a fixture or data file under a test folder is low."""
    if not TEST_RE.search(_slashes(path)):
        return
    if _is_test_fixture(path):
        risks.append(Risk("low", "test_deleted", f"Deleted a test fixture ({_basename(path)})", index))
    else:
        risks.append(Risk("high", "test_deleted", f"Deleted a test file ({_basename(path)})", index))


# ---------------------------------------------------------------- shell commands

@dataclass
class _Word:
    value: str            # quotes removed; a backslash is kept unless it escapes a shell character
    start: int            # offsets in the prepared command text
    end: int
    quoted: bool
    role: str = "word"    # word | assign | text | write | read | skip
    delete: bool = False  # an argument of a delete command


def _name(word: str) -> str:
    n = _basename(word).lower()
    return n[:-4] if n.endswith(".exe") else n


def _is_switch(t: str) -> bool:
    """Windows switches such as /F, //IM, /MT:8. Real root folders (/tmp, /etc) are not switches."""
    m = _SWITCH_RE.match(t)
    return bool(m) and m.group(1).lower() not in _POSIX_ROOT_NAMES


def _strip_heredocs(cmd: str, ps: bool) -> str:
    """The command without heredoc or here-string bodies (the operator line is kept)."""
    out: List[str] = []
    queue: List[Tuple[str, bool]] = []  # (terminator line, strip leading tabs)
    for line in cmd.split("\n"):
        if queue:
            term, strip = queue[0]
            probe = line.rstrip("\r")
            if strip:
                probe = probe.lstrip("\t")
            if probe == term:
                queue.pop(0)
            continue
        out.append(line)
        if ps:
            m = _PS_HERE_START_RE.search(line)
            if m:
                queue.append((m.group(1) + "@", False))
        else:
            for m in _HEREDOC_RE.finditer(line):
                if line[:m.start()].count("'") % 2 == 0:
                    queue.append((m.group(3), m.group(1) == "-"))
    return "\n".join(out)


def _scan(text: str, ps: bool) -> Tuple[List[tuple], List[Tuple[int, int]]]:
    """Words, separators and redirections of a prepared command, plus comment spans."""
    items: List[tuple] = []
    comments: List[Tuple[int, int]] = []
    n = len(text)
    esc = "`" if ps else "\\"
    i = 0
    while i < n:
        c = text[i]
        if c in " \t\r":
            i += 1
            continue
        if c == "#" and (i == 0 or text[i - 1] in " \t\r\n;|&()"):
            j = text.find("\n", i)
            j = n if j < 0 else j
            comments.append((i, j))
            i = j
            continue
        if c in "\n;|()" or (c == "&" and text[i + 1:i + 2] != ">"):
            j = i + 1
            if c in "|&" and text[j:j + 1] == c:
                j += 1
            items.append(("sep", i, j))
            i = j
            continue
        if c in "<>" or text[i:i + 2] == "&>":
            start = i
            prev = items[-1] if items else None
            if (c in "<>" and prev is not None and prev[0] == "word" and prev[1].end == i
                    and prev[1].value.isdigit() and not prev[1].quoted):
                start = prev[1].start
                items.pop()
            m = _REDIR_RE.match(text, i)
            if m is None:  # cannot happen for < > and &>, kept for safety
                i += 1
                continue
            i = m.end()
            items.append(("redir", m.group(0), start, i))
            continue
        start = i
        buf: List[str] = []
        quoted = False
        while i < n and text[i] not in " \t\r\n;|&()<>":
            ch = text[i]
            if ch == "'":
                j = text.find("'", i + 1)
                j = n if j < 0 else j
                buf.append(text[i + 1:j])
                quoted = True
                i = j + 1
            elif ch == '"':
                quoted = True
                i += 1
                while i < n and text[i] != '"':
                    if text[i] == esc and i + 1 < n and (ps or text[i + 1] in '"\\$`\n'):
                        buf.append(text[i + 1])
                        i += 2
                    else:
                        buf.append(text[i])
                        i += 1
                i += 1
            elif ch == esc and i + 1 < n:
                nxt = text[i + 1]
                if nxt == "\n":
                    i += 2
                elif ps or nxt in " \t;&|()<>'\"":
                    buf.append(nxt)
                    i += 2
                else:
                    buf.append(ch)
                    i += 1
            else:
                buf.append(ch)
                i += 1
        i = min(i, n)
        items.append(("word", _Word("".join(buf), start, i, quoted)))
    return items, comments


def _redir_role(op: str, value: str) -> str:
    if op in ("<<", "<<-", "<<<"):
        return "skip"  # heredoc delimiter or here-string text
    if op in (">&", "<&"):
        if value.isdigit() or value == "-":
            return "skip"  # 2>&1 and similar file-descriptor copies
        return "write" if op == ">&" else "read"
    return "read" if op.startswith("<") else "write"


def _simple_commands(items: List[tuple]) -> List[List[_Word]]:
    cmds: List[List[_Word]] = []
    cur: List[_Word] = []
    pending: Optional[str] = None
    for it in items:
        if it[0] == "sep":
            if cur:
                cmds.append(cur)
            cur, pending = [], None
        elif it[0] == "redir":
            pending = it[1]
        else:
            w = it[1]
            if pending is not None:
                w.role = _redir_role(pending, w.value)
                pending = None
            cur.append(w)
    if cur:
        cmds.append(cur)
    return cmds


def _mark_message_args(words: List[_Word]) -> None:
    """git commit -m "text": the message is text, not a path."""
    take = False
    for w in words:
        if take:
            w.role, take = "text", False
        elif w.value.startswith("--message="):
            w.role = "text"
        elif w.value in ("-m", "--message") or _MSG_FLAG_RE.match(w.value):
            take = True


def _plan(words: List[_Word], ps: bool) -> Optional[str]:
    """Give each word of one simple command its role and mark the arguments of delete
    commands. Returns the command name (lower case, no .exe), or None."""
    plain = [w for w in words if w.role == "word"]
    j, wrapped = 0, False
    while j < len(plain):
        v = plain[j].value
        if not ps and _ASSIGN_RE.match(v):
            plain[j].role = "assign"
        elif _name(v) in _WRAPPERS:
            wrapped = True
        elif not (wrapped and v.startswith("-")):
            break
        j += 1
    if j >= len(plain):
        return None
    name = _name(plain[j].value)
    rest = plain[j + 1:]
    delete_from: Optional[int] = None
    if name in _DELETE_CMDS:
        delete_from = j + 1
    if name in _TEXT_CMDS:
        for w in rest:
            w.role = "text"
    elif name == "git":
        sub_at = next((q for q in range(j + 1, len(plain)) if not plain[q].value.startswith("-")), None)
        sub = plain[sub_at].value.lower() if sub_at is not None else ""
        if sub == "rm":
            delete_from = sub_at + 1
        elif sub in ("commit", "tag"):
            _mark_message_args(plain[sub_at + 1:])
    elif name in _INTERPRETERS:
        take = False
        for w in rest:
            if take:
                w.role, take = "text", False
            elif w.value in _CODE_FLAGS:
                take = True
    if delete_from is None:
        for q in range(j + 1, len(plain) - 1):
            if plain[q].value in ("-exec", "-execdir", "-ok") and _name(plain[q + 1].value) in _DELETE_CMDS:
                delete_from = q + 2
                break
    if delete_from is not None:
        for w in plain[delete_from:]:
            if w.role == "word":
                w.delete = True
    return name


def _expand(value: str, assigns: Dict[str, Optional[str]]) -> Optional[str]:
    """`value` with $NAME, ${NAME} and $env:NAME replaced. None when a variable is unknown
    or the value runs a command ($( or a backtick)."""
    if "$(" in value or "`" in value:
        return None
    if "$" not in value:
        return value
    unknown: List[str] = []

    def rep(m: "re.Match") -> str:
        name = m.group(1) or m.group(2) or m.group(3)
        if name in assigns:
            if assigns[name] is not None:
                return assigns[name]  # type: ignore[return-value]
        elif name.upper() in _RESOLVABLE_ENV:
            env = os.environ.get(name.upper())
            if env:
                return env
        unknown.append(name)
        return m.group(0)

    out = _VAR_RE.sub(rep, value)
    return None if unknown else out


def _check_shell_word(w: _Word, name: str, cwd: Optional[str], workdir: Optional[str],
                      extra: List["re.Pattern"], assigns: Dict[str, Optional[str]],
                      step: Step, risks: List[Risk]) -> None:
    mode = "delete" if w.delete else ("write" if w.role == "write" else "read")
    raw = w.value
    if raw.startswith("-"):
        if "=" not in raw:
            return
        raw = raw.split("=", 1)[1]  # --db=/x -> /x
    if not raw or raw == "--" or _is_switch(raw) or _URL_RE.match(raw) or _is_device(raw):
        return
    if name in ("sed", "perl") and _SED_RE.match(raw) and raw.count(raw[1]) >= 3:
        return  # s/old/new/ expression, not a path
    exp = _expand(raw, assigns)
    texts = list(dict.fromkeys(t for t in (exp, raw) if t is not None))
    for t in texts:
        if _is_secret(t, extra):
            risks.append(Risk("high", "secret_file", f"Touched a secrets file from the shell ({_basename(t)})", step.index))
            break
    if mode == "delete":
        for t in texts:
            if TEST_RE.search(_slashes(t)):
                _test_deleted(t, step.index, risks)
                break
    if exp is None or len(exp) <= 1:
        return  # unknown variable: no path check
    path_like = _looks_like_path_arg(exp) or bool(_DOTDOT_RE.search(exp)) or "/" in exp or "\\" in exp or exp.startswith(".")
    if mode != "delete" and not path_like:
        return  # a bare word such as a command name or an option value
    target = os.path.expanduser(exp)
    if not _outside(target, cwd, workdir):
        return
    shown = _resolved(target, workdir)
    if mode == "write":
        risks.append(Risk("low", "shell_outside", f"Shell command wrote outside the working folder ({shown})", step.index))
    elif mode == "delete":
        risks.append(Risk("high", "shell_outside", f"Shell command referenced a path outside the working folder ({shown})", step.index))
    else:
        risks.append(Risk("low", "shell_outside", f"Shell command referenced a path outside the working folder ({shown})", step.index))


def _code_view(text: str, comments: List[Tuple[int, int]], text_words: List[_Word]) -> str:
    """The command text the dangerous-command rules read: comments removed, and quoted text
    arguments (echo, git commit -m, python -c) blanked. Unquoted $VAR words are kept."""
    spans = [(s, e, "") for s, e in comments]
    for w in text_words:
        if w.quoted or not w.value.startswith("$") or "$(" in w.value:
            spans.append((w.start, w.end, '""'))
    out = text
    for s, e, rep in sorted(spans, key=lambda x: x[0], reverse=True):
        out = out[:s] + rep + out[e:]
    return out


def _shell_risks(step: Step, cwd: Optional[str], extra: List["re.Pattern"], risks: List[Risk]) -> None:
    cmd = str(step.input.get("command") or "")
    ps = step.tool == "PowerShell"

    wd = step.input.get("workdir")
    workdir = wd.strip() if isinstance(wd, str) and _is_abs(wd.strip()) else None
    if workdir and cwd and _outside(workdir, cwd):
        risks.append(Risk("medium", "shell_outside", f"Ran a command outside the working folder ({workdir})", step.index))

    prepared = _strip_heredocs(cmd, ps)
    items, comments = _scan(prepared, ps)
    cmds = _simple_commands(items)
    names = [_plan(words, ps) for words in cmds]
    text_words = [w for words in cmds for w in words if w.role == "text"]
    code = _code_view(prepared, comments, text_words)

    rules = list(_DANGEROUS)
    if ps:
        rules += _PS_RULES
    for rx, sev, why in rules:
        if rx.search(code):
            risks.append(Risk(sev, "command", why, step.index))
    if _SUDO_RE.search(code):
        risks.append(Risk("high", "command", "Ran a command with sudo", step.index))

    assigns: Dict[str, Optional[str]] = {}
    for words, name in zip(cmds, names):
        for w in words:
            if w.role == "assign":
                var, _, val = w.value.partition("=")
                assigns[var] = _expand(val, assigns)
            elif w.role in ("word", "write", "read"):
                _check_shell_word(w, name or "", cwd, workdir, extra, assigns, step, risks)


# ---------------------------------------------------------------- policy

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
            verb = {"Read": "Read", "Delete": "Deleted"}.get(tool, "Modified")
            risks.append(Risk("high", "secret_file", f"{verb} a secrets file ({_basename(p)})", step.index))
        if (tool in WRITE_TOOLS or tool in DELETE_TOOLS) and _outside(p, cwd):
            verb = "Deleted" if tool in DELETE_TOOLS else "Wrote"
            risks.append(Risk("high", "write_outside", f"{verb} outside the working folder ({p})", step.index))
        elif _outside(p, cwd):
            risks.append(Risk("low", "read_outside", f"Read outside the working folder ({p})", step.index))

    if tool in DELETE_TOOLS:
        for p in paths:
            _test_deleted(p, step.index, risks)

    if (tool in WRITE_TOOLS or tool in DELETE_TOOLS) and paths and _GIT_DIR_RE.search(_slashes(paths[0])):
        verb = "Deleted" if tool in DELETE_TOOLS else "Wrote"
        risks.append(Risk("medium", "git_internals", f"{verb} inside the .git folder ({_basename(paths[0])})", step.index))

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


def _score(risks: List[Risk]) -> int:
    """Points for a run: each severity is totalled separately (low and medium are capped,
    high is not), and the sum is capped at 100. A repeat of a code adds a third of its points."""
    counted: Dict[Tuple[str, str], int] = {}
    totals = {"high": 0, "medium": 0, "low": 0}
    for r in risks:
        k = (r.code, r.severity)
        counted[k] = counted.get(k, 0) + 1
        base = SEVERITY_POINTS[r.severity]
        totals[r.severity] += base if counted[k] == 1 else base // 3
    total = totals["high"] + min(MEDIUM_POINTS_CAP, totals["medium"]) + min(LOW_POINTS_CAP, totals["low"])
    return min(100, total)


def assess(run: Run) -> Tuple[int, str, List[Risk]]:
    policy = load_policy(run.cwd)
    all_risks: List[Risk] = []
    for s in run.steps:
        s.risks = assess_step(s, run.cwd, policy)
        all_risks.extend(s.risks)
    score = _score(all_risks)
    level = "Low" if score < 25 else "Medium" if score < 60 else "High"
    return score, level, all_risks
