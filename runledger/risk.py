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

Files a Bash command writes (shell_writes: redirects, heredocs, tee, cp, mv, sed -i, Python
open(..., "w")) get the edit-tool checks: secrets file (high), .git folder (medium), migration
file (medium) and outside the working folder (low, as for other shell writes). Copying or
moving a project file into a temp folder is low (temp_copy); other writes to temp are not
flagged. Changing a local database file (sqlite3 X.db with write SQL or a script on stdin,
Python sqlite3 code that runs DDL or DML) or running a migration tool (migrate.py, manage.py
migrate, alembic, prisma, rails/rake, knex, ...) is medium (db_write). Read-only queries are not.

Migration files (migrations/, db/migrate/, alembic/versions/, prisma/migrations/, V<n>__*.sql)
written by any tool are medium (db_migration). The Git working tree, when known, tells an
added migration from a changed one.

Delete tool (canonical "Delete" with file_path) is checked like rm of that path.
Write, Edit, MultiEdit and Delete inside a .git folder are medium (git_internals).

MCP tools (mcp__<server>__<tool>, or the bare <server>__<tool> names Factory Droid reports for
connector tools) are graded by their verb (see mcp_risk): a destructive word is high, a
state-changing word is medium, a read verb or an unknown name stays low. An execute-class call
is graded by its SQL argument instead, and the SQL body never leaves the call.

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
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from . import paths
try:
    from . import secrets
except Exception:  # pragma: no cover - fallback when runledger.secrets is unavailable
    secrets = None  # type: ignore[assignment]
from .mcp_risk import classify_mcp, mcp_parts
from .parser import Run, Step
from .ps_writes import ps_writes, shell_kind
from .shell_syntax import ansi_c_end, strip_heredocs as _posix_strip_heredocs
from .shell_writes import database_writes, shell_writes
from .testdiff import SKIP_RE, TestChange, analyze as analyze_test_diffs
from .userconfig import user_config_path as _user_config_path

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


_FALLBACK_SECRET_PATTERNS = [
    r"(^|/)\.env(\.[\w-]+)?$", r"(^|/)\.envrc$", r"id_(rsa|ed25519|ecdsa|dsa)(_[\w-]+)?$",
    r"\.(pem|key|p8|p12|pfx|jks|keystore|kdbx|keychain)$", r"\.tfstate(\.backup)?$", r"\.tfvars$",
    r"(^|/)\.(npmrc|pypirc|netrc|pgpass|htpasswd|vault-token|dockercfg|git-credentials)$",
    r"(^|/)\.git/config$", r"(^|/)\.aws/(credentials|config)$", r"(^|/)\.ssh/",
    r"(^|/)(credentials(\.json)?|client_secret[^/]*\.json|service[-_]account[^/]*\.json|"
    r"secrets?\.(ya?ml|json|toml)|master\.key)$",
    r"(^|/)\.docker/config\.json$", r"(^|/)\.kube/config$",
]
_FALLBACK_PUBLIC_PATH_RE = re.compile(
    r"(?:\.pub$|(?:^|/)\.env\.(?:example|sample|template|dist|schema|defaults|test|ci)$"
    r"|\.tfvars\.(?:example|sample|template)$)",
    re.I,
)
SECRET_PATTERNS = (
    [rx.pattern for rx in secrets.SECRET_PATH_PATTERNS]
    if secrets is not None else list(_FALLBACK_SECRET_PATTERNS)
)
_SECRET_RE = re.compile("|".join(SECRET_PATTERNS), re.I)
_ENV_EXAMPLE_RE = (
    secrets._PUBLIC_OR_TEMPLATE_PATH_RE
    if secrets is not None else _FALLBACK_PUBLIC_PATH_RE
)

TEST_RE = re.compile(r"(^|/)(tests?|__tests__|spec|specs)(/|$)|(\.|_|-)(test|spec)\.[a-z]+$|(^|/)test_[^/]+\.py$", re.I)
# Real test source: deleting it is high. Anything else under a test folder is data (see _is_test_fixture).
_TEST_SOURCE_RE = re.compile(r"^test_[^/]*\.py$|[._-](test|spec)\.[a-z0-9]+$", re.I)
_FIXTURE_DIRS = {"fixtures", "fixture", "testdata", "__snapshots__", "__fixtures__", "snapshots"}
_DATA_EXTS = (".json", ".jsonl", ".ndjson", ".txt", ".md", ".csv", ".tsv", ".yaml", ".yml", ".xml",
              ".html", ".snap", ".log", ".db", ".sqlite", ".sql", ".toml", ".ini")
_GIT_DIR_RE = re.compile(r"(^|/)\.git/", re.I)
# Schema migration files of common tools; __init__.py in a migrations package is not one.
_MIGRATION_RE = re.compile(
    r"(^|/)(?:migrations?|db/migrate|alembic/versions|prisma/migrations)/(?:[^/]+/)*"
    r"(?!__init__\.py$)[^/]+\.(?:sql|py|rb|js|mjs|cjs|ts|go)$"
    r"|(^|/)V\d+(?:[._]\d+)*__[^/]+\.sql$", re.I)
_MIGRATION_VERB = re.compile(r"^Wrote a database migration \((.+)\)$")

WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
DELETE_TOOLS = ("Delete",)
SHELL_TOOLS = ("Bash", "PowerShell")

# The codes of findings that come from a rule the user or a project wrote down
# (`command_rules`/`tool_rules`). `rule_deny` is high, `rule_ask` is medium, and the guard
# always keeps both on its effective lists, so a rule hit is enforced whatever else the
# lists say.
RULE_ASK_CODE = "rule_ask"
RULE_DENY_CODE = "rule_deny"
RULE_CODES = (("deny", RULE_DENY_CODE, "high"), ("ask", RULE_ASK_CODE, "medium"))

# Shell rules shared by Bash and PowerShell steps, matched against the command text
# with heredoc bodies, comments and text arguments removed (see _code_view).
# Flag rules use lookaheads limited to one command (stops at ; & | newline), so the
# flags may come in any order. A shell separator ends an option list like whitespace or
# the end of the command does, so a flag right before `;`, `&&`, `|` or a newline matches.
DANGEROUS_CMDS: List[Tuple[str, str, str]] = [
    (r"\brm\b(?=[^;&|\n]*\s(?:-[a-zA-Z]*[rR][a-zA-Z]*|--recursive)(?=\s|$|[;&|]))"
     r"(?=[^;&|\n]*\s(?:-[a-zA-Z]*f[a-zA-Z]*|--force)(?=\s|$|[;&|]))",
     "high", "Recursive force delete (rm -rf)"),
    (r"\bgit\s+push\b[^;&|\n]*?(?:(?<!\S)-[a-zA-Z]*f[a-zA-Z]*(?=\s|$|[;&|])"
     r"|--force(?:-with-lease|-if-includes)?(?=\s|$|[;&|])|(?<=\s)\+\S)",
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
    # Deleting more than the command says it does: force-deleting a branch, dropping the stash,
    # wiping a remote branch, a container or cluster, a registry key, a device or a whole tree.
    (r"\bgit\s+branch\b(?=[^;&|\n]*\s-[a-zA-Z]*(?-i:D)[a-zA-Z]*(?=\s|$|[;&|]))",
     "medium", "Force-deleted a git branch (git branch -D)"),
    (r"\bgit\s+branch\b(?=[^;&|\n]*\s--delete(?=\s|$|[;&|]))(?=[^;&|\n]*\s(?:--force|-f)(?=\s|$|[;&|]))",
     "medium", "Force-deleted a git branch"),
    (r"\bgit\s+push\b[^;&|\n]*?(?:\s--delete(?=\s|$|[;&|])|\s-d(?=\s|$|[;&|])|\s:[^\s:]\S*)",
     "high", "Deleted a remote git branch or tag"),
    (r"\bgit\s+checkout\b(?:[^;&|\n]*\s)?--\s+\.(?=\s|$|[;&|])|\bgit\s+checkout\s+\.(?=\s|$|[;&|])",
     "medium", "Discarded all working-tree changes (git checkout .)"),
    (r"\bgit\s+restore\b(?![^;&|\n]*\s--staged(?![^;&|\n]*\s(?:--worktree|-[a-zA-Z]*W)))"
     r"[^;&|\n]*\s\.(?=\s|$|[;&|])",
     "medium", "Discarded all working-tree changes (git restore .)"),
    (r"\bgit\s+stash\s+(?:drop|clear)\b", "medium", "Dropped git stash entries"),
    (r"\bfind\b[^;&|\n]*\s-delete\b|\bfind\b[^;&|\n]*\s-exec\s+(?:rm|unlink|shred)\b",
     "medium", "Deleted files with find"),
    (r"\bdd\b[^;&|\n]*\sof=/dev/(?!null\b|zero\b|stdout\b|stderr\b)\S+",
     "high", "Wrote raw data to a device (dd of=/dev/...)"),
    (r"\bdd\b[^;&|\n]*\sof=(?!/dev/)\S+", "medium", "Overwrote a file with dd"),
    (r"\bdocker\s+(?:system|builder|image|container|network)\s+prune\b(?![^;&|\n]*--volumes)",
     "medium", "Pruned Docker resources"),
    (r"\bdocker\s+volume\s+prune\b|\bdocker\s+system\s+prune\b[^;&|\n]*--volumes",
     "high", "Pruned Docker volumes (data loss)"),
    (r"\baws\s+s3\s+rm\b[^;&|\n]*\s--recursive\b|\baws\s+s3\s+rb\b[^;&|\n]*\s--force\b",
     "high", "Deleted S3 objects/buckets recursively"),
    (r"\baws\s+s3\s+rm\b(?![^;&|\n]*\s--recursive\b)", "medium", "Deleted an S3 object"),
    (r"\bgh\s+repo\s+delete\b", "high", "Deleted a GitHub repository"),
    (r"\bgh\s+(?:release|issue|gist|label|secret|variable|cache|run|workflow)\s+delete\b",
     "medium", "Deleted GitHub resources"),
    (r"\bhelm\s+(?:uninstall|delete)\b", "high", "Uninstalled a Helm release"),
    (r"\bdelete\s+from\s+\S+(?![^;&|\n]*\bwhere\b)", "high", "SQL DELETE without WHERE"),
    (r"\brsync\b(?![^;&|\n]*(?:\s--dry-run\b|\s-[a-zA-Z]*(?-i:n)[a-zA-Z]*(?=\s|$)))"
     r"[^;&|\n]*\s--(?:delete(?:-[a-z]+)?|del|remove-source-files)\b",
     "medium", "rsync that deletes files"),
    (r"\breg(?:\.exe)?\s+delete\b(?![^;&|\n]*\b(?:hklm|hkey_local_machine)\b)",
     "medium", "Deleted a registry key"),
    (r"\breg(?:\.exe)?\s+delete\b[^;&|\n]*\b(?:hklm|hkey_local_machine)\b",
     "high", "Deleted a machine-wide registry key"),
    (r"\b(?:rd|rmdir)\b(?=[^;&|\n]*\s/s\b)(?=[^;&|\n]*\s/q\b)",
     "high", "Recursive quiet delete (rd /s /q)"),
]
_DANGEROUS = [(re.compile(p, re.I), sev, why) for p, sev, why in DANGEROUS_CMDS]

# PowerShell-only rules (applied in addition to DANGEROUS_CMDS for PowerShell steps).
PS_CMDS: List[Tuple[str, str, str]] = [
    (r"\b(?:remove-item|ri|del|erase|rd|rmdir)\b(?=[^;&|\n]*\s-rec\w*)(?=[^;&|\n]*\s-fo\w*)",
     "high", "Recursive force delete (Remove-Item -Recurse -Force)"),
    (r"\b(?:remove-item|ri|del|erase|rd|rmdir)\b(?=[^;&|\n]*\s-rec\w*)(?![^;&|\n]*\s-fo\w*)",
     "medium", "Recursive delete (Remove-Item -Recurse)"),
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


@dataclass(frozen=True)
class _ArgSpec:
    """Which options of a search or stream-editing command take a pattern/script ("pattern"),
    a file to read ("file") or some other value ("value"). Unlisted options are flags."""
    short: Dict[str, str]
    long: Dict[str, str]
    positional_pattern: bool = True  # the first operand is the pattern unless -e/-f gave one
    skip_assigns: bool = False       # awk: NAME=value operands are variable assignments


_GREP_SPEC = _ArgSpec(
    short={"e": "pattern", "f": "file", "A": "value", "B": "value", "C": "value", "m": "value",
           "d": "value", "D": "value"},
    long={"--regexp": "pattern", "--file": "file", "--exclude-from": "file",
          "--after-context": "value", "--before-context": "value", "--context": "value",
          "--max-count": "value", "--include": "value", "--exclude": "value",
          "--exclude-dir": "value", "--label": "value", "--binary-files": "value",
          "--devices": "value", "--directories": "value", "--group-separator": "value"})
_RG_SPEC = _ArgSpec(
    short={"e": "pattern", "f": "file", "A": "value", "B": "value", "C": "value", "m": "value",
           "g": "value", "t": "value", "T": "value", "E": "value", "j": "value", "M": "value",
           "r": "value", "d": "value"},
    long={"--regexp": "pattern", "--file": "file", "--ignore-file": "file",
          "--after-context": "value", "--before-context": "value", "--context": "value",
          "--max-count": "value", "--glob": "value", "--iglob": "value", "--type": "value",
          "--type-not": "value", "--type-add": "value", "--type-clear": "value",
          "--encoding": "value", "--threads": "value", "--max-columns": "value",
          "--replace": "value", "--max-depth": "value", "--max-filesize": "value",
          "--sort": "value", "--sortr": "value", "--color": "value", "--colors": "value",
          "--engine": "value", "--path-separator": "value", "--pre": "value",
          "--pre-glob": "value", "--context-separator": "value",
          "--field-match-separator": "value", "--field-context-separator": "value"})
_AG_SPEC = _ArgSpec(
    short={"A": "value", "B": "value", "C": "value", "m": "value", "G": "value", "g": "value",
           "p": "file"},
    long={"--after": "value", "--before": "value", "--context": "value", "--max-count": "value",
          "--file-search-regex": "value", "--ignore": "value", "--ignore-dir": "value",
          "--path-to-ignore": "file", "--depth": "value", "--match": "pattern",
          "--type": "value", "--type-set": "value", "--type-add": "value", "--output": "value"})
_GIT_GREP_SPEC = _ArgSpec(
    short={"e": "pattern", "f": "file", "A": "value", "B": "value", "C": "value", "m": "value"},
    long={"--file": "file", "--after-context": "value", "--before-context": "value",
          "--context": "value", "--max-count": "value", "--max-depth": "value",
          "--threads": "value"})
_SED_SPEC = _ArgSpec(
    short={"e": "pattern", "f": "file", "l": "value"},
    long={"--expression": "pattern", "--file": "file", "--line-length": "value"})
_AWK_SPEC = _ArgSpec(
    short={"f": "file", "e": "pattern", "v": "value", "F": "value"},
    long={"--file": "file", "--source": "pattern", "--assign": "value",
          "--field-separator": "value"},
    skip_assigns=True)
_SEARCH_SPECS = {"grep": _GREP_SPEC, "egrep": _GREP_SPEC, "fgrep": _GREP_SPEC, "zgrep": _GREP_SPEC,
                 "rg": _RG_SPEC, "ag": _AG_SPEC, "ack": _AG_SPEC}
_SCRIPT_SPECS = {"sed": _SED_SPEC, "gsed": _SED_SPEC, "awk": _AWK_SPEC, "gawk": _AWK_SPEC,
                 "mawk": _AWK_SPEC, "nawk": _AWK_SPEC}
_FIND_PATTERN_OPTS = {"-name", "-iname", "-path", "-ipath", "-wholename", "-iwholename",
                      "-regex", "-iregex", "-lname", "-ilname"}
_PERL_CODE_RE = re.compile(r"^-[A-Za-z]*[eE]$")
# Quoted text with regex-only syntax (alternation, anchors, .*, groups) is a pattern, not a path.
_REGEX_TEXT_RE = re.compile(r"\||^\^|\.\*|\(\?|\[\^")

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


_is_windows_style = paths.is_windows_style
_is_abs = paths.is_abs
_key = paths.key
_within = paths.within


def _path_key(p: str) -> str:
    """Comparable form of a path for the per-step `shell_outside` de-duplication: Windows
    paths compare case-insensitively with either separator, POSIX paths compare exactly."""
    return _key(p, _is_windows_style(p))


def _in_temp(target: str, self_only: bool = False, cwd: Optional[str] = None) -> bool:
    for env_var in ("HOME", "USERPROFILE"):
        h = os.environ.get(env_var)
        if h and _within(target, h) and not _APPDATA_TEMP_RE.search(target[len(h):]):
            return False
    if not self_only and _APPDATA_TEMP_RE.search(target) and not (cwd and _APPDATA_TEMP_RE.search(cwd)):
        return True
    roots = list(_POSIX_TEMP_ROOTS)
    for var in ("TMPDIR", "TEMP", "TMP"):
        v = os.environ.get(var)
        if v:
            roots.append(v)
    try:
        roots.append(tempfile.gettempdir())
    except OSError:  # no usable temp directory
        pass
    # Filesystem roots (e.g. TMPDIR=/) or temp dirs containing cwd itself would make sibling folders "temp"; skip them.
    return any(
        _within(target, r)
        for r in roots
        if r and not _WIN_ROOT_RE.match(r) and not (cwd and _within(cwd, r))
    )


def _is_device(p: str) -> bool:
    """Device and special files (/dev/null, NUL, /proc/self/...) are never paths in a project."""
    p = paths.strip_long_prefix(p)
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


def _resolve_real(p: str) -> str:
    """Resolve symlinks and junctions on the local filesystem, including broken links."""
    real = paths.strip_long_prefix(os.path.realpath(p))
    if os.name == "nt":
        probe = ntpath.normpath(p)
        tail: List[str] = []
        while probe and not os.path.exists(probe):
            if os.path.islink(probe):
                try:
                    dest = paths.strip_long_prefix(os.readlink(probe))
                    if not _is_abs(dest):
                        dest = ntpath.join(ntpath.dirname(probe), dest)
                    resolved = paths.strip_long_prefix(os.path.realpath(dest))
                    for part in reversed(tail):
                        resolved = ntpath.join(resolved, part)
                    return ntpath.normpath(resolved)
                except OSError:
                    break
            parent, name = ntpath.split(probe)
            if not name or parent == probe:
                break
            tail.append(name)
            probe = parent
    return real


def _escapes_via_realpath(target: str, cwd: str) -> bool:
    """True when `target` is lexically inside `cwd`, both exist locally (not UNC, matching
    host OS style), and resolving symlinks/junctions places `target` outside `cwd`."""
    if paths.is_unc(target) or paths.is_unc(cwd):
        return False
    host_win = os.name == "nt"
    if _is_windows_style(cwd) != host_win or _is_windows_style(target) != host_win:
        return False
    rel = paths.relative(target, cwd)
    if not rel or rel == ".":
        return False
    first = rel.split("/", 1)[0]
    try:
        if not os.path.lexists(os.path.join(cwd, first)):
            return False
        real_cwd = _resolve_real(cwd)
        real_target = _resolve_real(target)
    except OSError:
        return False
    return not _within(real_target, real_cwd)


def _outside(path: str, cwd: Optional[str], base: Optional[str] = None, self_temp_only: bool = False) -> bool:
    """True if `path` resolves outside `cwd`. Temp directories count as inside.
    A relative path is resolved against `base` (a working directory) when given,
    otherwise against `cwd`. Windows-style paths (drive letter or UNC on either side)
    compare case-insensitively with either separator; POSIX paths compare exactly."""
    if not cwd or not path or _is_device(path) or not _is_abs(cwd):
        return False  # a relative working folder (some logs record ".") cannot place a path
    path = paths.strip_long_prefix(path)
    cwd = paths.strip_long_prefix(cwd)
    if base:
        base = paths.strip_long_prefix(base)
    win = _is_windows_style(cwd)
    path = _msys_to_windows(path, win)
    if base:
        base = _msys_to_windows(base, win)
    mod = ntpath if _is_windows_style(path, cwd, base) else posixpath
    # _is_abs, not mod.isabs: ntpath.isabs("/tmp") is False on Python 3.13+
    target = path if _is_abs(path) else mod.join(base or cwd, path)
    if not _within(target, cwd):
        return not _in_temp(target, self_only=self_temp_only, cwd=cwd)
    return _escapes_via_realpath(target, cwd)


def _resolved(path: str, base: Optional[str]) -> str:
    """The path as it is shown in a reason: relative paths joined onto `base` when given."""
    if not base or _is_abs(path):
        return path
    if _is_windows_style(base):
        return ntpath.join(base, path.replace("/", "\\"))
    return posixpath.join(base, path)


def _is_secret(path: str, extra: Optional[List["re.Pattern"]] = None) -> bool:
    if secrets is not None:
        return secrets.is_secret_path(path, extra)
    if not isinstance(path, str) or not path:
        return False
    norm = path.replace("\\", "/")
    if _ENV_EXAMPLE_RE.search(norm):
        return False
    if _SECRET_RE.search(norm):
        return True
    return bool(extra and any(rx.search(norm[:1024]) for rx in extra))


def _user_regexes(patterns: Any) -> List["re.Pattern"]:
    if secrets is not None:
        compiled, _ = secrets.compile_extra_secret_paths(patterns)
        return compiled
    return []


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
    if secrets is not None:
        return secrets.find_secrets(text)
    if not isinstance(text, str) or not text:
        return []
    kinds = [kind for rx, kind in _CONTENT_SECRET_RULES if rx.search(text)]
    for m in _GENERIC_SECRET_RE.finditer(text):
        kinds.append(m.group(1).lower().replace("_", " ").replace("-", " "))
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


def is_test_source(path: str) -> bool:
    """A test file with test code in it, not a fixture or data file under a test folder."""
    return bool(TEST_RE.search(_slashes(path))) and not _is_test_fixture(path)


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
    base: Optional[str] = None  # alternate base for relative paths (for example `git -C`)


def _name(word: str) -> str:
    n = _basename(word).lower()
    return n[:-4] if n.endswith(".exe") else n


def _is_switch(t: str) -> bool:
    """Windows switches such as /F, //IM, /MT:8. Real root folders (/tmp, /etc) are not switches."""
    m = _SWITCH_RE.match(t)
    return bool(m) and m.group(1).lower() not in _POSIX_ROOT_NAMES


def _strip_heredocs(cmd: str, ps: bool) -> str:
    """The command without heredoc or here-string bodies (the operator line is kept)."""
    if not ps:
        stripped, _ = _posix_strip_heredocs(cmd)
        return stripped
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
        m = _PS_HERE_START_RE.search(line)
        if m:
            queue.append((m.group(1) + "@", False))
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
            if ch == "$" and text[i + 1:i + 2] == "'":
                end = ansi_c_end(text, i)
                j = n if end == -1 else end - 1
                buf.append(text[i + 2:j])
                quoted = True
                i = n if end == -1 else end
                continue
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


def _mark_pattern_args(words: List[_Word], spec: _ArgSpec, pattern_role: str) -> None:
    """Give the pattern or script of grep/rg/ag/git grep/sed/awk `pattern_role` and other
    option values "skip", so only operands in file positions are checked as paths."""
    roles = {"pattern": pattern_role, "value": "skip", "file": "word"}
    have_pattern = False
    options = True
    i = 0
    while i < len(words):
        w = words[i]
        v = w.value
        kind: Optional[str] = None
        attached = False
        if options and v == "--":
            options = False
        elif options and v.startswith("--") and len(v) > 2:
            key, eq, _ = v.partition("=")
            kind = spec.long.get(key)
            attached = bool(eq)
        elif options and v.startswith("-") and len(v) > 1:
            for k in range(1, len(v)):
                kind = spec.short.get(v[k])
                if kind:
                    attached = k + 1 < len(v)
                    break
        elif spec.skip_assigns and have_pattern and _ASSIGN_RE.match(v):
            w.role = "skip"
        elif spec.positional_pattern and not have_pattern:
            w.role = pattern_role
            have_pattern = True
        if kind:
            have_pattern = have_pattern or kind in ("pattern", "file")
            if attached:
                if kind != "file":
                    w.role = roles[kind]
            elif i + 1 < len(words):
                words[i + 1].role = roles[kind]
                i += 1
        i += 1


def _join_base(current: Optional[str], value: str) -> str:
    """Apply a git -C/work-tree path to the current git base without using host OS semantics."""
    if _is_abs(value) or not current:
        return value
    mod = ntpath if _is_windows_style(current, value) else posixpath
    return mod.normpath(mod.join(current, value))


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
        q = j + 1
        git_base: Optional[str] = None
        while q < len(plain):
            value = plain[q].value
            if value == "--":
                q += 1
                break
            if value in ("-C", "--work-tree"):
                if q + 1 < len(plain):
                    git_base = _join_base(git_base, plain[q + 1].value)
                    q += 2
                    continue
                q += 1
                continue
            if value.startswith("--work-tree="):
                git_base = _join_base(git_base, value.split("=", 1)[1])
                q += 1
                continue
            if value in ("-c", "--git-dir", "--namespace", "--super-prefix", "--config-env"):
                q += 2
                continue
            if value.startswith("-"):
                q += 1
                continue
            break
        sub_at = q if q < len(plain) else None
        sub = plain[sub_at].value.lower() if sub_at is not None else ""
        if sub == "rm":
            delete_from = sub_at + 1
            for w in plain[delete_from:]:
                w.base = git_base
        elif sub in ("commit", "tag"):
            _mark_message_args(plain[sub_at + 1:])
        elif sub == "grep":
            _mark_pattern_args(plain[sub_at + 1:], _GIT_GREP_SPEC, "text")
    elif name in _SEARCH_SPECS:
        _mark_pattern_args(rest, _SEARCH_SPECS[name], "text")
    elif name in _SCRIPT_SPECS:
        # Not "text": a sed or awk script can run commands, so the dangerous-command rules
        # still read it.
        _mark_pattern_args(rest, _SCRIPT_SPECS[name], "skip")
    elif name == "find" and any(w.value == "-delete" for w in rest):
        # `find ROOT... -delete` recursively removes matches below its search roots.
        # Mark only the starting paths; expressions beginning with '-' do not name files.
        for w in rest:
            if w.value.startswith("-") or w.value in ("!", "(", ")"):
                break
            w.delete = True
    elif name in _INTERPRETERS:
        take = False
        for w in rest:
            if take:
                w.role, take = "text", False
            elif w.value in _CODE_FLAGS or (name in ("perl", "ruby") and _PERL_CODE_RE.match(w.value)):
                take = True
    if name == "find":
        # A -name/-path pattern is a glob or regex the search matches against, never a command,
        # so the dangerous-command rules read the pattern as text (the _code_view blanks it).
        for q, w in enumerate(rest[:-1]):
            if w.value in _FIND_PATTERN_OPTS:
                rest[q + 1].role = "text"
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
                      step: Step, risks: List[Risk], flagged: Set[str]) -> None:
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
    if mode == "read" and w.quoted and _REGEX_TEXT_RE.search(raw):
        return  # a quoted regex for a command not listed in _SEARCH_SPECS
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
    base = workdir
    if w.base:
        expanded_base = _expand(w.base, assigns)
        if expanded_base is not None:
            base = (expanded_base if _is_abs(expanded_base)
                    else _resolved(expanded_base, workdir or cwd))
    if not _outside(target, cwd, base):
        return
    shown = _resolved(target, base)
    flagged.add(_path_key(shown))
    if mode == "write":
        risks.append(Risk("low", "shell_outside", f"Shell command wrote outside the working folder ({shown})", step.index))
    elif mode == "delete":
        risks.append(Risk("high", "shell_outside", f"Shell command referenced a path outside the working folder ({shown})", step.index))
    else:
        risks.append(Risk("low", "shell_outside", f"Shell command referenced a path outside the working folder ({shown})", step.index))


def _code_view(text: str, comments: List[Tuple[int, int]], text_words: List[_Word]) -> str:
    """The command text the dangerous-command rules read: comments removed, and text
    arguments (echo, git commit -m, python -c, a `find -name` pattern) blanked. Unquoted
    $VAR words are kept."""
    spans = [(s, e, "") for s, e in comments]
    for w in text_words:
        if w.quoted or not w.value.startswith("$") or "$(" in w.value:
            spans.append((w.start, w.end, '""'))
    out = text
    for s, e, rep in sorted(spans, key=lambda x: x[0], reverse=True):
        out = out[:s] + rep + out[e:]
    return out


def _shell_risks(step: Step, cwd: Optional[str], extra: List["re.Pattern"], risks: List[Risk],
                 default_workdir: Optional[str] = None,
                 policy: Optional[Dict[str, Any]] = None) -> Set[str]:
    """Risks for the words of a shell command. Returns the paths reported as outside this step
    (`_path_key` form), so `_shell_write_risks` does not report the same write twice."""
    cmd = str(step.input.get("command") or "")
    ps = shell_kind(step) == "powershell"

    wd = step.input.get("workdir")
    explicit_wd = wd.strip() if isinstance(wd, str) and _is_abs(wd.strip()) else None
    # Paths this step already reported as outside. `_shell_write_risks` compares against the
    # paths themselves, so a write that the word check already named is not reported twice.
    flagged: Set[str] = set()
    if explicit_wd and cwd and _outside(explicit_wd, cwd):
        risks.append(Risk("medium", "shell_outside", f"Ran a command outside the working folder ({explicit_wd})", step.index))
        flagged.add(_path_key(explicit_wd))
    sub_wd = default_workdir if (default_workdir and default_workdir != cwd and _is_abs(default_workdir)) else None
    workdir = explicit_wd or sub_wd

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
    risks.extend(command_rule_risks(cmd, code, policy, step.index))

    assigns: Dict[str, Optional[str]] = {}
    for words, name in zip(cmds, names):
        if ps and any(
            not w.quoted and (w.value.lower() == "-whatif" or w.value.lower().startswith("-whatif:"))
            and not w.value.lower().endswith("$false")
            for w in words
        ):
            continue
        for w in words:
            if w.role == "assign":
                var, _, val = w.value.partition("=")
                assigns[var] = _expand(val, assigns)
            elif w.role in ("word", "write", "read"):
                _check_shell_word(w, name or "", cwd, workdir, extra, assigns, step, risks, flagged)
    return flagged


def _project_path(path: str, cwd: Optional[str]) -> str:
    """`path` relative to the working folder when it lies inside it, as receipts list files."""
    return paths.relative(path, cwd)


def _is_migration(path: str) -> bool:
    return bool(_MIGRATION_RE.search(_slashes(path)))


def _migration_risk(path: str, verb: str, cwd: Optional[str], index: int, risks: List[Risk]) -> None:
    if _is_migration(path) and not _outside(path, cwd):
        risks.append(Risk("medium", "db_migration", f"{verb} a database migration ({_project_path(path, cwd)})", index))


def _shell_write_risks(step: Step, cwd: Optional[str], extra: List["re.Pattern"], risks: List[Risk],
                       default_workdir: Optional[str] = None, flagged: Optional[Set[str]] = None) -> None:
    """The files a shell command writes (shell_writes / ps_writes) get the checks edit-tool
    writes get, and commands that change a database file or run migrations are reported.
    `flagged` holds the paths `_shell_risks` already reported as outside (`_path_key` form)."""
    cmd = str(step.input.get("command") or "")
    wd = step.input.get("workdir")
    base = wd.strip() if isinstance(wd, str) and _is_abs(wd.strip()) else (default_workdir or cwd)
    is_ps = shell_kind(step) == "powershell"
    writes = ps_writes(cmd) if is_ps else shell_writes(cmd)
    moved_away = {w.path for w in writes if w.deleted}
    for w in writes:
        target = _resolved(w.path, base)
        verb = "Deleted" if w.deleted else "Wrote"
        if w.deleted:
            _test_deleted(w.path, step.index, risks)
        if _is_secret(w.path, extra):
            risks.append(Risk("high", "secret_file", f"Touched a secrets file from the shell ({_basename(w.path)})", step.index))
        if _GIT_DIR_RE.search(_slashes(target)):
            risks.append(Risk("medium", "git_internals", f"{verb} inside the .git folder ({_basename(target)})", step.index))
        if not w.deleted:
            _migration_risk(target, "Wrote", cwd, step.index, risks)
        if not w.deleted and _outside(target, cwd, self_temp_only=True):
            if _path_key(target) not in flagged:
                risks.append(Risk("low", "shell_outside", f"Shell command wrote outside the working folder ({target})", step.index))
        elif w.source and cwd and _is_abs(target) and _in_temp(target, self_only=True):
            source = _resolved(w.source, base)
            if not _outside(source, cwd) and not _in_temp(source):
                how = "Moved" if w.source in moved_away else "Copied"
                risks.append(Risk("low", "temp_copy", f"{how} a project file outside the project "
                                  f"({_project_path(source, cwd)} → {target})", step.index))
    for d in database_writes(cmd):
        if d.kind == "migrate":
            shown = d.command if len(d.command) <= 80 else d.command[:77] + "..."
            risks.append(Risk("medium", "db_write", f"Ran a database migration command ({shown})", step.index))
        elif d.target:
            target = _resolved(d.target, base)
            risks.append(Risk("medium", "db_write", f"Wrote to a local database file ({_project_path(target, cwd)})", step.index))


# ---------------------------------------------------------------- policy

_LOW_DEFAULT_KINDS = frozenset({
    "mcp", "read_outside", "shell_outside", "temp_copy", "network", "package_install",
})
_HIGH_DEFAULT_KINDS = frozenset({
    "secret_file", "secret_in_content", "secret", "write_outside", "outside_repo",
    "test_deleted", "test_skipped", "destructive_delete", "git_force_push",
    "pipe_to_shell", "destructive_sql", "package_publish", "infra_change", "sudo",
})


def _read_json_dict(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError, RecursionError):
        return {}
    return data if isinstance(data, dict) else {}


def _is_project_override_raising(kind: str, level: str) -> bool:
    """True when a project `severity_overrides` entry raises severity above default."""
    k = kind.strip().lower()
    lv = level.strip().lower()
    if lv == "low":
        return False
    if lv == "medium":
        return k in _LOW_DEFAULT_KINDS
    if lv == "high":
        return k not in _HIGH_DEFAULT_KINDS
    return False


def _project_ignored_or_lowered_kinds(cwd: Optional[str], refused: Optional[Set[str]] = None) -> List[str]:
    """Distinct risk kinds a project `.runledger.json` tried to ignore or lower.

    `refused` holds the overrides `_apply_policy` refused because they would have lowered a
    finding's actual severity (the static check below only knows each kind's default)."""
    if not cwd or paths.is_unc(cwd):
        return []
    data = _read_json_dict(Path(cwd) / POLICY_FILE)
    if not data:
        return []
    rejected: List[str] = []
    ignore = data.get("ignore")
    if isinstance(ignore, list):
        for s in ignore:
            if isinstance(s, str) and s.strip():
                rejected.append(s.strip().lower())
    overrides = data.get("severity_overrides")
    if isinstance(overrides, dict):
        for code, level in overrides.items():
            if not isinstance(code, str) or not code.strip():
                continue
            if not isinstance(level, str) or level.lower() not in SEVERITY_POINTS:
                continue
            if (not _is_project_override_raising(code, level)
                    or code.strip().lower() in (refused or ())):
                rejected.append(code.strip().lower())
    return list(dict.fromkeys(rejected))


def load_policy(cwd: Optional[str]) -> Dict[str, Any]:
    """Effective policy combining user config (`RUNLEDGER_CONFIG` or `~/.runledger/config.json`)
    and project config (`<cwd>/.runledger.json`).

    Trust model:
    - User config may set `ignore`, `severity_overrides` (raise or lower), and `extra_secret_paths`.
    - Project config may only add safe `extra_secret_paths` and raise severities in
      `severity_overrides`; project `ignore` entries are ignored and a project override is
      applied only when it is higher than the severity of the finding it matches, which is
      checked per finding in `_apply_policy` (the project's own values are carried in
      `project_severity_overrides`).
    - `extra_secret_paths` are validated with `secrets.safe_user_regex` and capped at 20.
    - `mcp_verbs`, `tool_rules` and `command_rules` are read from the PROJECT file only: a
      receipt is reproducible from the repository it belongs to, so the user's own rules
      (which the live guard does enforce) are not re-applied to an old transcript. Project
      verbs may add `destructive`/`write` words, never `read`.
    """
    policy: Dict[str, Any] = {"ignore": [], "severity_overrides": {}, "extra_secret_paths": [],
                              "mcp_verbs": parse_mcp_verbs({}), "tool_rules": parse_tool_rules({}),
                              "command_rules": parse_command_rules({})[0]}
    user_data = _read_json_dict(_user_config_path())
    proj_data = _read_json_dict(Path(cwd) / POLICY_FILE) if (cwd and not paths.is_unc(cwd)) else {}

    ignore = user_data.get("ignore")
    if isinstance(ignore, list):
        policy["ignore"] = [s.strip() for s in ignore if isinstance(s, str) and s.strip()]

    merged_overrides: Dict[str, str] = {}
    project_only: Dict[str, str] = {}
    proj_overrides = proj_data.get("severity_overrides")
    if isinstance(proj_overrides, dict):
        for code, level in proj_overrides.items():
            if (isinstance(code, str) and code.strip()
                    and isinstance(level, str) and level.lower() in SEVERITY_POINTS):
                project_only[code.strip()] = level.lower()

    user_overrides = user_data.get("severity_overrides")
    if isinstance(user_overrides, dict):
        for code, level in user_overrides.items():
            if isinstance(code, str) and code.strip() and isinstance(level, str) and level.lower() in SEVERITY_POINTS:
                key = code.strip()
                merged_overrides[key] = level.lower()
                project_only.pop(key, None)
    for code, level in project_only.items():
        merged_overrides.setdefault(code, level)
    policy["severity_overrides"] = merged_overrides
    if project_only:
        policy["project_severity_overrides"] = project_only

    max_extra = getattr(secrets, "MAX_EXTRA_SECRET_PATHS", 20)
    raw_extra: List[str] = []
    for src in (user_data.get("extra_secret_paths"), proj_data.get("extra_secret_paths")):
        if isinstance(src, list):
            for rx in src:
                if isinstance(rx, str) and rx and _compiles(rx) and rx not in raw_extra:
                    raw_extra.append(rx)
                    if len(raw_extra) >= max_extra:
                        break
        if len(raw_extra) >= max_extra:
            break
    policy["extra_secret_paths"] = raw_extra
    policy["mcp_verbs"] = parse_mcp_verbs(proj_data.get("mcp_verbs"), allow_read=False)
    policy["tool_rules"] = parse_tool_rules(proj_data.get("tool_rules"))
    policy["command_rules"] = parse_command_rules(proj_data.get("command_rules"), POLICY_FILE)[0]
    return policy


def _compiles(rx: str) -> bool:
    if secrets is not None:
        return secrets.safe_user_regex(rx) is not None
    return False


def parse_command_rules(value: Any, source: str = "the config file"
                        ) -> Tuple[Dict[str, List[Dict[str, Any]]], List[str]]:
    """The `command_rules` of one config file: `{"deny": [...], "ask": [...]}`.

    Each entry is a regular expression, or an object `{"pattern", "reason", "raw"}`. The
    expression is matched case-insensitively against the quote/heredoc-blanked command; a
    `raw: true` entry reads the whole command instead. A pattern that is not a safe regular
    expression is dropped, and the warning names it and the file it came from."""
    rules: Dict[str, List[Dict[str, Any]]] = {"deny": [], "ask": []}
    warnings: List[str] = []
    if not isinstance(value, dict):
        return rules, warnings
    for kind in ("deny", "ask"):
        entries = value.get(kind)
        for entry in entries if isinstance(entries, list) else []:
            pattern = entry.get("pattern") if isinstance(entry, dict) else entry
            reason = entry.get("reason") if isinstance(entry, dict) else None
            if not isinstance(pattern, str) or not pattern.strip():
                continue
            compiled = secrets.safe_user_regex(pattern) if secrets is not None else None
            if compiled is None:
                warnings.append(f"Dropped invalid command_rules.{kind} pattern {pattern.strip()!r} in {source}: "
                                f"it is not a safe regular expression.")
                continue
            rules[kind].append({"rx": compiled,
                                "label": reason.strip() if isinstance(reason, str) and reason.strip()
                                else pattern.strip(),
                                "raw": bool(entry.get("raw")) if isinstance(entry, dict) else False})
    return rules, warnings


def parse_tool_rules(value: Any) -> Dict[str, List[str]]:
    """The `tool_rules` of one config file: `{"deny": [...], "ask": [...]}`.

    Every entry is an `fnmatch` glob on the whole tool name, matched case-sensitively."""
    rules: Dict[str, List[str]] = {"deny": [], "ask": []}
    if not isinstance(value, dict):
        return rules
    for kind in ("deny", "ask"):
        entries = value.get(kind)
        for entry in entries if isinstance(entries, list) else []:
            if isinstance(entry, str) and entry.strip():
                rules[kind].append(entry.strip())
    return rules


def parse_mcp_verbs(value: Any, allow_read: bool = True) -> Dict[str, List[str]]:
    """The `mcp_verbs` of one config file: `{"destructive": [...], "write": [...], "read": [...]}`.

    A project file may extend `destructive` and `write` only: with `allow_read` false its
    `read` list is dropped, because a read word lowers a tool that carries it."""
    verbs: Dict[str, List[str]] = {"destructive": [], "write": [], "read": []}
    if not isinstance(value, dict):
        return verbs
    for key in verbs:
        if key == "read" and not allow_read:
            continue
        words = value.get(key)
        if isinstance(words, list):
            verbs[key] = [w.strip().lower() for w in words if isinstance(w, str) and w.strip()]
    return verbs


def merge_mcp_verbs(*parts: Dict[str, List[str]]) -> Dict[str, List[str]]:
    """The union of the `mcp_verbs` of several config files, each word kept once."""
    out: Dict[str, List[str]] = {"destructive": [], "write": [], "read": []}
    for part in parts:
        for key in out:
            for word in (part or {}).get(key) or []:
                if word not in out[key]:
                    out[key].append(word)
    return out


def _rule_matched_risks(kind: str, code: str, severity: str, matched: List[str], index: int) -> List[Risk]:
    return [Risk(severity, code, f"Matched {kind} rule: {label}", index) for label in matched]


def command_rule_risks(command: str, view: str, policy: Optional[Dict[str, Any]], index: int) -> List[Risk]:
    """Risks for the `command_rules` of the user's file and the project's file.

    `view` is the quote/heredoc-blanked command the built-in rules read; an entry with
    `raw: true` reads `command` itself, so a rule can also match quoted text."""
    rules = policy.get("command_rules") if isinstance(policy, dict) else None
    risks: List[Risk] = []
    if not isinstance(rules, dict):
        return risks
    for kind, code, severity in RULE_CODES:
        hits = [r["label"] for r in rules.get(kind) or [] if r["rx"].search(command if r["raw"] else view)]
        risks.extend(_rule_matched_risks(kind, code, severity, hits, index))
    return risks


def tool_rule_risks(tool: str, policy: Optional[Dict[str, Any]], index: int) -> List[Risk]:
    """Risks for the `tool_rules` of the user's file and the project's file.

    The globs match the whole tool name, case-sensitively. A tool that is on both lists
    carries both findings, so the guard denies it: deny beats ask."""
    rules = policy.get("tool_rules") if isinstance(policy, dict) else None
    risks: List[Risk] = []
    if not isinstance(rules, dict):
        return risks
    for kind, code, severity in RULE_CODES:
        hits = [p for p in rules.get(kind) or [] if fnmatchcase(tool, p)]
        risks.extend(_rule_matched_risks(kind, code, severity, hits, index))
    return risks


def _risk_matches_key(r: Risk, key: str, allow_substring: bool = False) -> bool:
    k = key.strip().lower()
    if not k:
        return False
    code = r.code.lower()
    reason = r.reason.lower()
    if k == code:
        return True
    if k == "outside_repo" and code in ("outside_repo", "write_outside", "read_outside", "shell_outside"):
        return True
    if k == "destructive_delete" and (code in ("destructive_delete", "delete") or "recursive force delete" in reason):
        return True
    if k == "git_force_push" and (code == "git_force_push" or "force-pushed to a git remote" in reason):
        return True
    if k == "git_push" and (code == "git_push" or reason == "pushed to a git remote"):
        return True
    if k == "network" and (code == "network" or "network request" in reason):
        return True
    if k == "secret" and code in ("secret", "secret_in_content"):
        return True
    if allow_substring and (k in code or k in reason):
        return True
    return False


def _apply_policy(risks: List[Risk], policy: Dict[str, Any],
                  refused: Optional[Set[str]] = None) -> List[Risk]:
    ignore = []
    for s in policy.get("ignore") or []:
        if isinstance(s, str) and s.strip():
            ignore.append(s.strip().lower())
    overrides = policy.get("severity_overrides") or {}
    project_only = policy.get("project_severity_overrides") or {}
    kept = []
    for r in risks:
        if any(_risk_matches_key(r, s, allow_substring=True) for s in ignore):
            continue
        for k, level in overrides.items():
            if isinstance(k, str) and isinstance(level, str) and level.lower() in SEVERITY_POINTS:
                if _risk_matches_key(r, k, allow_substring=False):
                    lv = level.lower()
                    if k in project_only and SEVERITY_POINTS[lv] < SEVERITY_POINTS[r.severity]:
                        # A project may raise a risk's severity, never lower it.
                        if refused is not None:
                            refused.add(k.lower())
                        continue
                    r.severity = lv
        kept.append(r)
    return kept


def assess_step(step: Step, cwd: Optional[str], policy: Optional[Dict[str, Any]] = None,
                workdir: Optional[str] = None, refused: Optional[Set[str]] = None) -> List[Risk]:
    """Risks for one step. `policy` defaults to `load_policy(cwd)`; `assess`
    passes it in so the config file is read once per run. `workdir` (when given and
    different from `cwd`) is the subfolder relative paths resolve against while `cwd`
    remains the project boundary. `refused` collects the project severity overrides that
    were refused as lowering."""
    if policy is None:
        policy = load_policy(cwd)
    extra = _user_regexes(policy.get("extra_secret_paths"))
    risks: List[Risk] = []
    tool = step.tool
    paths = _paths_in_step(step)
    base = workdir if (workdir and workdir != cwd and _is_abs(workdir)) else None

    for p in paths:
        if _is_secret(p, extra):
            verb = {"Read": "Read", "Delete": "Deleted"}.get(tool, "Modified")
            risks.append(Risk("high", "secret_file", f"{verb} a secrets file ({_basename(p)})", step.index))
        if (tool in WRITE_TOOLS or tool in DELETE_TOOLS) and _outside(p, cwd, base=base):
            verb = "Deleted" if tool in DELETE_TOOLS else "Wrote"
            risks.append(Risk("high", "write_outside", f"{verb} outside the working folder ({p})", step.index))
        elif _outside(p, cwd, base=base):
            risks.append(Risk("low", "read_outside", f"Read outside the working folder ({p})", step.index))

    if tool in DELETE_TOOLS:
        for p in paths:
            _test_deleted(p, step.index, risks)

    if (tool in WRITE_TOOLS or tool in DELETE_TOOLS) and paths and _GIT_DIR_RE.search(_slashes(paths[0])):
        verb = "Deleted" if tool in DELETE_TOOLS else "Wrote"
        risks.append(Risk("medium", "git_internals", f"{verb} inside the .git folder ({_basename(paths[0])})", step.index))

    if tool in ("Edit", "MultiEdit") and paths and TEST_RE.search(_slashes(paths[0])):
        edits = step.input.get("edits")
        edits = [e for e in edits if isinstance(e, dict)] if isinstance(edits, list) and edits else [step.input]
        olds = [e.get("old_string") if isinstance(e.get("old_string"), str) else "" for e in edits]
        news = [e.get("new_string") if isinstance(e.get("new_string"), str) else "" for e in edits]
        removed = sum(len(o.splitlines()) for o in olds)
        added = sum(len(n.splitlines()) for n in news)
        text = " ".join(olds)
        new_text = " ".join(news)
        asserts_removed = len(re.findall(r"\b(assert|expect)\b", text)) - len(re.findall(r"\b(assert|expect)\b", new_text))
        skip_added = SKIP_RE.search(new_text) and not SKIP_RE.search(text)
        if skip_added:
            risks.append(Risk("high", "test_skipped", f"Disabled or skipped tests in {_basename(paths[0])}", step.index))
        elif asserts_removed >= 2 or (removed - added) >= 15:
            risks.append(Risk("medium", "test_weakened", f"Removed assertions from {_basename(paths[0])}", step.index))

    if tool in ("Write", "Edit", "MultiEdit") and paths:
        _migration_risk(_resolved(paths[0], base) if base else paths[0],
                        "Wrote" if tool == "Write" else "Changed", cwd, step.index, risks)

    if tool in WRITE_TOOLS:
        name = _basename(paths[0]) if paths else "a file"
        for text in _written_texts(step):
            for kind in _hardcoded_secret_kinds(text):
                risks.append(Risk("high", "secret_in_content", f"Wrote a hardcoded {kind} into {name}", step.index))

    if tool in SHELL_TOOLS:
        flagged = _shell_risks(step, cwd, extra, risks, default_workdir=base, policy=policy)
        _shell_write_risks(step, cwd, extra, risks, default_workdir=base, flagged=flagged)

    risks.extend(tool_rule_risks(tool, policy, step.index))

    if mcp_parts(tool) is not None:
        severity, reason = classify_mcp(tool, step.input, policy)
        risks.append(Risk(severity, "mcp", reason, step.index))

    # de-duplicate by (code, reason)
    seen = set()
    uniq = []
    for r in risks:
        key = (r.code, r.reason)
        if key not in seen:
            seen.add(key)
            uniq.append(r)
    return _apply_policy(uniq, policy, refused)


def _score(risks: List[Risk]) -> int:
    """Points for a run: each severity is totalled separately (low and medium are capped,
    high is not), and the sum is capped at 100. A repeat of a code adds a third of its points."""
    counted: Dict[Tuple[str, ...], int] = {}
    totals = {"high": 0, "medium": 0, "low": 0}
    distinct_high = 0
    for r in risks:
        k: Tuple[str, ...] = (r.code, r.reason if r.code == "command" else "", r.severity)
        counted[k] = counted.get(k, 0) + 1
        base = SEVERITY_POINTS[r.severity]
        if counted[k] == 1:
            totals[r.severity] += base
            if r.severity == "high":
                distinct_high += 1
        else:
            totals[r.severity] += base // 3
    if distinct_high >= 3:
        totals["high"] = max(totals["high"], 100)
    total = totals["high"] + min(MEDIUM_POINTS_CAP, totals["medium"]) + min(LOW_POINTS_CAP, totals["low"])
    return min(100, total)


def _written_paths(step: Step, cwd: Optional[str]) -> List[str]:
    """Paths a step wrote, relative to cwd when inside it (the keys of Run.test_diffs)."""
    if step.tool in WRITE_TOOLS:
        paths = _paths_in_step(step)[:1]
    elif step.tool in SHELL_TOOLS:
        cmd = str(step.input.get("command") or "")
        writes = ps_writes(cmd) if shell_kind(step) == "powershell" else shell_writes(cmd)
        paths = [w.path for w in writes if not w.deleted]
    else:
        return []
    out = []
    for p in paths:
        p = _project_path(_resolved(p, cwd), cwd)
        out.append(posixpath.normpath(_slashes(p)) if not _is_abs(p) else p)
    return out


def _apply_test_verdicts(run: Run, changes: List[TestChange], policy: Dict[str, Any],
                         refused: Optional[Set[str]] = None) -> None:
    """A test file's Git diff verdict replaces what the per-step Edit rule said about it,
    so one file never reads as both weakened and replaced."""
    verdicts = {c.path: c for c in changes}
    if not verdicts:
        return
    last_writer: Dict[str, Step] = {}
    for s in run.steps:
        written = [p for p in _written_paths(s, run.cwd) if p in verdicts]
        for p in written:
            last_writer[p] = s
        if written and s.tool in ("Edit", "MultiEdit"):
            s.risks = [r for r in s.risks if r.code not in ("test_weakened", "test_skipped")]
    for path, change in verdicts.items():
        step = last_writer.get(path)
        if step is None:
            continue
        name = _basename(path)
        if change.verdict == "skipped":
            risk = Risk("high", "test_skipped", f"Disabled or skipped tests in {name}", step.index)
        elif change.verdict == "weakened" and change.removed_tests:
            risk = Risk("medium", "test_weakened",
                        f"Removed {', '.join(change.removed_tests)} from {name} with no replacement", step.index)
        elif change.verdict == "weakened":
            risk = Risk("medium", "test_weakened", f"Removed assertions from {name}", step.index)
        else:
            continue
        step.risks.extend(_apply_policy([risk], policy, refused))


def assess(run: Run) -> Tuple[int, str, List[Risk]]:
    policy = load_policy(run.cwd)
    refused_project: Set[str] = set()
    all_risks: List[Risk] = []
    for s in run.steps:
        s.risks = assess_step(s, run.cwd, policy, refused=refused_project)
        for r in s.risks:
            # The Git working tree tells a new migration from an edited one.
            m = _MIGRATION_VERB.match(r.reason) if r.code == "db_migration" else None
            status = (run.git_changes.get(m.group(1)) or {}).get("status") if m else None
            if status:
                verb = "Added" if status in ("untracked", "added") else "Changed"
                r.reason = f"{verb} a database migration ({m.group(1)})"
    run.test_verdicts = analyze_test_diffs(run.test_diffs, run.prompts)
    _apply_test_verdicts(run, run.test_verdicts, policy, refused_project)
    for s in run.steps:
        all_risks.extend(s.risks)
    rejected = _project_ignored_or_lowered_kinds(run.cwd, refused_project)
    if rejected:
        note = (
            f"Project config tried to ignore/lower {len(rejected)} risk kind(s) "
            "(not applied; set in user config to enable)."
        )
        if note not in run.notes:
            run.notes.append(note)
    score = _score(all_risks)
    level = "Low" if score < 25 else "Medium" if score < 60 else "High"
    return score, level, all_risks
