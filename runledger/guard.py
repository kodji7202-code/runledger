"""Real-time policy guard: the PreToolUse hook of Claude Code, Factory Droid and Codex CLI.

The agent runs `runledger guard` before each tool call and writes the call to
stdin as JSON. The guard scores the call with the rules in risk.py, decides
allow / ask / deny, can ask a team approval server, and appends each decision to
<cwd>/.runledger/guard.log.

The installed hook carries its agent in `--agent claude-code|droid|codex`, so the call is
read with the right tool vocabulary (Droid `Execute`/`Create`/`Edit`/`ApplyPatch`, Codex
`apply_patch`, the MCP names of both) and answered in the shape the agent understands.
Without the flag the agent is inferred from the payload's own fields. Codex cannot prompt
from a hook, so an ask becomes a deny there with the reason why (the user's own file may
opt out with `"codex_ask": "allow"`).

Two settings files, not equal:
  ~/.runledger/config.json  the user's own file. It may set everything.
  <cwd>/.runledger.json     the project's file. It may only TIGHTEN the guard.

The effective settings are the user's (or the defaults), tightened by the project's:
  mode         enforce, unless the user says monitor and the project does not say enforce
  fail_closed  on if either file says so
  deny, ask    the user's (or default) list, plus the project's list. Each list's own
               "!" exclusions apply only to that list, so a project cannot remove a
               user or default rule
  severity     a project may raise a risk's severity, never lower it. Its ignore list is not used
  approval     server and key come only from RUNLEDGER_SERVER / RUNLEDGER_API_KEY or the
               user file. The project may set timeout_s; on_timeout "ask" needs the user's consent
Each project setting that would loosen the guard is ignored and written to guard.log as a warning.

Hook output (stdout; the exit code is always 0):
  deny   -> permissionDecision "deny". Claude reads the reason and adapts.
  ask    -> permissionDecision "ask". Claude Code shows its normal confirmation.
  allow  -> printed only after an explicit approval from the approval server.
            A plain allow prints nothing, so Claude Code's own permission rules
            still apply. A hook that answers "allow" skips the user's permission
            prompts, and this guard is meant to restrict, not to grant.
Internal errors print nothing (fail open) unless fail_closed is on. In monitor mode
nothing is ever blocked.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import labels
from . import paths as _paths
from . import risk as _risk
from .mcp_risk import mcp_parts
from .parser import Step
from .risk import POLICY_FILE, RULE_ASK_CODE, RULE_DENY_CODE, SEVERITY_POINTS, Risk, assess_step
# The user's own file has one reader for every consumer (guard, risk, rollback, doctor).
from .userconfig import USER_CONFIG_DIR, USER_CONFIG_FILE, load_user_config, user_config_path

try:
    from . import secrets as _secrets
    from .secrets import find_secrets, is_secret_path, redact_text, safe_user_regex
except Exception:  # pragma: no cover - standalone fallback when runledger.secrets is unavailable
    _secrets = None  # type: ignore[assignment]
    find_secrets = None  # type: ignore[assignment]
    is_secret_path = None  # type: ignore[assignment]
    redact_text = None  # type: ignore[assignment]
    safe_user_regex = None  # type: ignore[assignment]

HOOK_COMMAND = "runledger guard"
HOOK_MARGIN_S = 15             # the hook entry allows the approval wait plus this many seconds
PROJECT_DIR_ENV_VARS = ("CLAUDE_PROJECT_DIR", "FACTORY_PROJECT_DIR", "DROID_PROJECT_DIR")

AGENT_CLAUDE = "claude-code"
AGENT_DROID = "droid"
AGENT_CODEX = "codex"
AGENTS = (AGENT_CLAUDE, AGENT_DROID, AGENT_CODEX)

# The agent's own tool name -> the canonical name every risk rule keys on. A native tool
# that is not in the table keeps its own name (it is logged with `canonical_tool: null`).
_DROID_TOOLS = {
    "Execute": "Bash",
    "Create": "Write",
    "Edit": "Edit",
    "Read": "Read",
    "LS": "Search",
    "Glob": "Search",
    "Grep": "Search",
    "Task": "Task",
    "FetchUrl": "WebFetch",
    "WebSearch": "WebSearch",
    "ApplyPatch": "ApplyPatch",
}
_CODEX_TOOLS = {"Bash": "Bash", "apply_patch": "ApplyPatch"}
_PATCH_TOOLS = ("ApplyPatch", "apply_patch")

LOG_DIR = ".runledger"
LOG_FILE = "guard.log"
KEY_ENV_DEFAULT = "RUNLEDGER_API_KEY"
SERVER_ENV = "RUNLEDGER_SERVER"
PROJECT_APPROVAL_KEYS = ("server", "api_key_env")  # a project file may never set these
TIMEOUT_DEFAULT_S = 120.0
TIMEOUT_MAX_S = 3600.0
POLL_INTERVAL_S = 2.0          # seconds between GETs while waiting for a decision
HTTP_TIMEOUT_S = 10.0          # socket timeout for a single request
MAX_POLL_ERRORS = 3            # consecutive transport errors before giving up
_MAX_RESPONSE_BYTES = 1 << 20

# Used when a list is absent from the user's file. The "!" entry keeps sudo out of the
# high-command deny rule; sudo still asks (via "severity:high").
DEFAULT_DENY = ["secret_in_content", "command:high", "!reason:ran a command with sudo"]
DEFAULT_ASK = ["severity:high"]

_LEVELS = ("high", "medium", "low")
REDACTED = "[REDACTED]"

_FALLBACK_SECRET_RES = [
    re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{12,}"),
    re.compile(r"(?<![A-Za-z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}(?![A-Za-z0-9])"),
    re.compile(r"(?<![A-Za-z0-9])(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{22,})"),
    re.compile(r"(?<![A-Za-z0-9])xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(r"(?<![A-Za-z0-9])[rs]k_(?:live|test)_[A-Za-z0-9]{16,}"),
    re.compile(r"(?<![A-Za-z0-9])AIza[0-9A-Za-z_-]{35}"),
    re.compile(r"(?<![A-Za-z0-9])glpat-[A-Za-z0-9_-]{20,}"),
    re.compile(r"(?<![A-Za-z0-9])npm_[A-Za-z0-9]{36}"),
    re.compile(r"(?<![A-Za-z0-9])pypi-[A-Za-z0-9_-]{32,}"),
    re.compile(r"(?<![A-Za-z0-9])hf_[A-Za-z0-9]{30,}"),
    re.compile(r"(?<![A-Za-z0-9_])rl_[A-Za-z0-9_-]{30,}"),
    re.compile(r"(?i)(?<![A-Za-z0-9])AccountKey\s*=\s*[A-Za-z0-9+/=]{40,}"),
    re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
]
_FALLBACK_GENERIC_RE = re.compile(
    r"(api[_-]?key|secret|token|password)[\"']?\s*[:=]\s*[\"']([^\"'\s]{8,})[\"']", re.I)

# Unquoted values, masked last. Only the named group "v" is replaced.
_SECRET_NAME = (r"(?:api[_-]?key|apikey|secret|token|passw(?:or)?d|credentials?"
                r"|private[_-]?key|access[_-]?key)")
_VALUE_SECRET_RES = [
    # TOKEN=abc123, export DB_PASSWORD=**** ?access_token=..., --password=...
    # The name parts are bounded so a long blob without "=" cannot cause quadratic backtracking.
    re.compile(r"(?i)(?<![A-Za-z0-9])[A-Za-z0-9_.-]{0,40}" + _SECRET_NAME
               + r"[A-Za-z0-9_.-]{0,40}\s*=\s*(?P<v>[^\s\"'&;|,)}\]]{6,})"),
    # --token abc123, --password hunter2
    re.compile(r"(?i)(?<![A-Za-z0-9-])--?(?:api[-_]?key|(?:access[-_]|auth[-_])?token|passw(?:or)?d"
               r"|(?:client[-_])?secret)\s+(?P<v>[^\s\"'-][^\s\"']{3,})"),
    # https://user:password@host
    re.compile(r"(?i)\b[a-z][a-z0-9+.-]{0,20}://[^\s/:@\"']{1,128}:(?P<v>[^\s/@\"']{3,256})@"),
    re.compile(r"(?i)\bauthorization:\s*basic\s+(?P<v>[A-Za-z0-9+/=]{8,})"),
]
_PLACEHOLDER_START = ("$", "%", "{", "<", "[", "(", "*")
_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

ApproverFn = Callable[[Dict[str, Any], Dict[str, Any]], str]


class ApprovalError(Exception):
    """The approval server gave no usable answer. The guard then asks locally."""


class InstallError(Exception):
    """settings.json cannot be merged safely. The file is left untouched."""


# ---------------------------------------------------------------- redaction

def _mask_value(m: "re.Match") -> str:
    val = m.group(2)
    if val.startswith(_PLACEHOLDER_START) or val.endswith((">", "...")):
        return m.group(0)
    whole = m.group(0)
    a, b = m.start(2) - m.start(0), m.end(2) - m.start(0)
    return whole[:a] + REDACTED + whole[b:]


def _looks_secret(value: str) -> bool:
    """An unquoted value worth masking: not a placeholder or a variable name, and either
    containing a digit or at least 12 characters long (so `token=refresh` stays readable)."""
    if value.startswith(_PLACEHOLDER_START) or value.isdigit():
        return False
    if _ENV_NAME_RE.match(value) and not any(c.isdigit() for c in value):
        return False
    return any(c.isdigit() for c in value) or len(value) >= 12


def _mask_named(m: "re.Match") -> str:
    value = m.group("v")
    if not _looks_secret(value):
        return m.group(0)
    whole = m.group(0)
    a, b = m.start("v") - m.start(0), m.end("v") - m.start(0)
    return whole[:a] + REDACTED + whole[b:]


def _secret_patterns() -> List["re.Pattern"]:
    """Patterns from secrets.py / risk.py when they are there, plus the fallbacks above."""
    try:
        from_secrets = [rx for rx, _kind in getattr(_secrets, "SECRET_CONTENT_PATTERNS", []) if hasattr(rx, "sub")]
    except (TypeError, ValueError):
        from_secrets = []
    return from_secrets + _FALLBACK_SECRET_RES


def redact(text: Any) -> str:
    """Mask secret values. Only the value is replaced, never the surrounding text.
    If redaction itself fails, the whole text is replaced."""
    s = "" if text is None else str(text)
    try:
        if redact_text is not None:
            return redact_text(s)
        for rx in [_FALLBACK_GENERIC_RE]:
            s = rx.sub(_mask_value, s)
        for rx in _secret_patterns():
            s = rx.sub(REDACTED, s)
        for rx in _VALUE_SECRET_RES:
            s = rx.sub(_mask_named, s)
        return s
    except Exception:
        return REDACTED


def clean(text: Any, limit: int = 200) -> str:
    """Redact, flatten to one line, then truncate. Redaction comes first so a
    secret cut by the limit cannot leak half of itself."""
    s = redact(text).replace("\r", " ").replace("\n", " ")
    if len(s) > limit:
        s = s[: max(0, limit - 3)] + "..."
    return s


# ---------------------------------------------------------------- settings files

def _section(data: Any, key: str) -> Dict[str, Any]:
    value = data.get(key) if isinstance(data, dict) else None
    return value if isinstance(value, dict) else {}


def project_root(event_or_cwd: Any = None) -> str:
    """Resolve the project root for a guard event or cwd string.

    Order:
    1. First set, non-empty, existing directory among CLAUDE_PROJECT_DIR,
       FACTORY_PROJECT_DIR, DROID_PROJECT_DIR.
    2. Walk up from the event's `cwd` (or `os.getcwd()`) to the first directory
       containing `.runledger.json` or `.git` (file or directory).
    3. The event's `cwd` (or `os.getcwd()`).
    """
    if isinstance(event_or_cwd, dict) or event_or_cwd is None:
        cwd = _event_cwd(event_or_cwd)
    elif isinstance(event_or_cwd, str) and event_or_cwd.strip():
        cwd = event_or_cwd.strip()
    else:
        cwd = os.getcwd()

    for env_var in PROJECT_DIR_ENV_VARS:
        val = (os.environ.get(env_var) or "").strip()
        if val and not _paths.is_unc(val) and _paths.is_abs(val):
            try:
                if os.path.isdir(val):
                    return str(Path(val))
            except OSError:
                pass

    if cwd and not _paths.is_unc(cwd) and _paths.is_abs(cwd):
        try:
            p = Path(cwd)
            for d in (p, *p.parents):
                if (d / ".git").exists() or (d / POLICY_FILE).exists():
                    return str(d)
        except OSError:
            pass
    return cwd


def read_policy(cwd: str, *, _resolved: bool = False) -> Dict[str, Any]:
    """The project's `<project_root>/.runledger.json` as a dict. Missing or invalid gives {}."""
    if not cwd or _paths.is_unc(cwd):
        return {}
    root = cwd if _resolved else project_root(cwd)
    if not root or _paths.is_unc(root):
        return {}
    try:
        data = json.loads((Path(root) / POLICY_FILE).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError, RecursionError):
        return {}
    return data if isinstance(data, dict) else {}


def trusted_approval_settings(user: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    """The approval server and the name of the variable that holds the key. Only the
    environment (RUNLEDGER_SERVER) and the user's file are trusted. The project file is
    never read for these: a cloned repository must not be able to send the key to its
    own server. The environment variable wins over the file for the server."""
    if user is None:
        user = load_user_config()
    server = (os.environ.get(SERVER_ENV) or "").strip()
    if not server and isinstance(user.get("server"), str):
        server = user["server"].strip()
    key_env = user["api_key_env"].strip() if isinstance(user.get("api_key_env"), str) else ""
    return {"server": server.rstrip("/"), "api_key_env": key_env or KEY_ENV_DEFAULT}


def _project_key_warnings(project: Dict[str, Any]) -> List[str]:
    approval = _section(_section(project, "guard"), "approval")
    warnings = [
        f"Ignored approval.{key} in {POLICY_FILE}: a project file cannot set the approval server or key. "
        f"Use {SERVER_ENV} / {KEY_ENV_DEFAULT} or {USER_CONFIG_DIR}/{USER_CONFIG_FILE} in your home folder."
        for key in PROJECT_APPROVAL_KEYS if key in approval
    ]
    read_words = _section(project, "mcp_verbs").get("read")
    if isinstance(read_words, list) and any(isinstance(w, str) and w.strip() for w in read_words):
        warnings.append(f"Ignored mcp_verbs.read in {POLICY_FILE}: a read word in this list would lower a "
                        f"destructive MCP tool, so a project may add destructive and write words only.")
    return warnings


# ---------------------------------------------------------------- rules

def _parse_rule(entry: Any) -> Optional[Dict[str, Any]]:
    """One list entry. Forms: `code`, `severity:<level>`, `reason:<text>`, `<code>:<level>`.
    A leading `!` is an exclusion: it removes matches from its own list only."""
    if not isinstance(entry, str):
        return None
    s = entry.strip()
    negate = s.startswith("!")
    if negate:
        s = s[1:].strip()
    low = s.lower()
    if low.startswith("severity:"):
        level = low[len("severity:"):].strip()
        return {"negate": negate, "kind": "severity", "value": level} if level in _LEVELS else None
    if low.startswith("reason:"):
        text = low[len("reason:"):].strip()
        return {"negate": negate, "kind": "reason", "value": text} if text else None
    if ":" in low:
        code, _, level = low.partition(":")
        code, level = code.strip(), level.strip()
        if code and level in _LEVELS:
            return {"negate": negate, "kind": "code_severity", "code": code, "value": level}
        return None
    return {"negate": negate, "kind": "code", "value": low} if low else None


def _parse_rules(value: Any, default: List[str]) -> List[Dict[str, Any]]:
    entries = default if not isinstance(value, list) else value
    out = []
    for entry in entries:
        rule = _parse_rule(entry)
        if rule is not None:
            rule["raw"] = entry.strip()
            out.append(rule)
    return out


def _rule_entry(code: str) -> Optional[Dict[str, Any]]:
    """The entry that keeps a user or project rule hit decided, added to every effective list."""
    rule = _parse_rule(code)
    if rule is not None:
        rule["raw"] = code
    return rule


def _rule_matches(rule: Dict[str, Any], risk: Risk) -> bool:
    kind = rule["kind"]
    if kind == "severity":
        return risk.severity == rule["value"]
    if kind == "reason":
        return rule["value"] in risk.reason.lower()
    if kind == "code":
        return _risk._risk_matches_key(risk, rule["value"])
    return _risk._risk_matches_key(risk, rule["code"]) and risk.severity == rule["value"]


def _matches_list(rules: List[Dict[str, Any]], risk: Risk) -> bool:
    """True when some positive entry matches and no `!` exclusion in the same list matches.

    A `rule_deny`/`rule_ask` hit is never excluded: the rule it comes from must be removed
    from the config instead, so one list entry cannot quietly switch a rule off."""
    if not any(_rule_matches(r, risk) for r in rules if not r["negate"]):
        return False
    if risk.code in (RULE_DENY_CODE, RULE_ASK_CODE):
        return True
    return not any(_rule_matches(r, risk) for r in rules if r["negate"])


# ---------------------------------------------------------------- effective settings

def _mode_of(section: Dict[str, Any]) -> Optional[str]:
    value = section.get("mode")
    value = value.lower() if isinstance(value, str) else ""
    return value if value in ("enforce", "monitor") else None


def _explicit_timeout(approval: Dict[str, Any]) -> Optional[float]:
    t = approval.get("timeout_s")
    if isinstance(t, (int, float)) and not isinstance(t, bool) and 0 < t:
        return min(float(t), TIMEOUT_MAX_S)   # NaN fails the comparison and counts as unset
    return None


def _on_timeout_of(approval: Dict[str, Any]) -> Optional[str]:
    value = approval.get("on_timeout")
    return value if value in ("deny", "ask") else None


def _effective_guard(project: Dict[str, Any], user: Dict[str, Any], trusted: Dict[str, str]) -> Tuple[Dict[str, Any], List[str]]:
    """The guard settings in force, and the warnings for project settings that were ignored."""
    p = _section(project, "guard")
    u = _section(user, "guard")
    pa = _section(p, "approval")
    ua = _section(u, "approval")
    warnings: List[str] = []

    # Mode: the user decides. A project may switch monitor to enforce, never the reverse.
    user_mode = _mode_of(u) or "enforce"
    project_mode = _mode_of(p)
    mode = "monitor" if user_mode == "monitor" and project_mode != "enforce" else "enforce"
    if project_mode == "monitor" and mode == "enforce":
        warnings.append(f"Ignored mode monitor in {POLICY_FILE}: a project can turn enforcement on, not off. "
                        f"Only {USER_CONFIG_DIR}/{USER_CONFIG_FILE} can set monitor mode.")

    if p.get("fail_closed") is False and u.get("fail_closed") is True:
        warnings.append(f"Ignored fail_closed false in {POLICY_FILE}: a project cannot turn fail_closed off.")

    # Lists: the user's (or default) list plus the project's. Each keeps its own exclusions.
    # Both lists end with the code of the findings a user or project rule produced, so a rule
    # hit is decided even when the user replaced the list the defaults would have come from.
    user_deny = _parse_rules(u.get("deny"), DEFAULT_DENY)
    user_ask = _parse_rules(u.get("ask"), DEFAULT_ASK)
    for rules, code in ((user_deny, RULE_DENY_CODE), (user_ask, RULE_ASK_CODE)):
        entry = _rule_entry(code)
        if entry is not None:
            rules.append(entry)
    project_deny = _parse_rules(p.get("deny"), [])
    project_ask = _parse_rules(p.get("ask"), [])

    # Timeouts: the project's timeout_s wins if set (it cannot loosen anything). on_timeout
    # "ask" needs the user's consent: a project cannot turn a deny into a local prompt.
    timeout = _explicit_timeout(pa)
    if timeout is None:
        timeout = _explicit_timeout(ua)
    if timeout is None:
        timeout = TIMEOUT_DEFAULT_S
    user_on, project_on = _on_timeout_of(ua), _on_timeout_of(pa)
    on_timeout = "ask" if user_on == "ask" and project_on != "deny" else "deny"
    if project_on == "ask" and on_timeout != "ask":
        warnings.append(f"Ignored on_timeout ask in {POLICY_FILE}: it would loosen deny. "
                        f"Only {USER_CONFIG_DIR}/{USER_CONFIG_FILE} can set it.")

    # Codex has no way to prompt from a hook, so an ask is denied there unless the user's own
    # file chooses to allow it. A project file may not make that choice.
    codex_ask = "allow" if u.get("codex_ask") == "allow" else "deny"
    if "codex_ask" in p:
        warnings.append(f"Ignored codex_ask in {POLICY_FILE}: Codex cannot prompt from a hook, so only "
                        f"{USER_CONFIG_DIR}/{USER_CONFIG_FILE} can decide what an ask means there.")

    cfg = {
        "mode": mode,
        "fail_closed": u.get("fail_closed") is True or p.get("fail_closed") is True,
        "deny": [user_deny, project_deny],
        "ask": [user_ask, project_ask],
        "user_deny": user_deny,
        "project_deny": project_deny,
        "user_ask": user_ask,
        "project_ask": project_ask,
        "codex_ask": codex_ask,
        "approval": {"server": trusted["server"], "api_key_env": trusted["api_key_env"],
                     "timeout_s": timeout, "on_timeout": on_timeout},
    }
    return cfg, warnings


def approval_timeout(project: Dict[str, Any], user: Optional[Dict[str, Any]] = None) -> float:
    """The approval wait an install plans for: the project's timeout_s, else the user's, else the default."""
    if user is None:
        user = load_user_config()
    t = _explicit_timeout(_section(_section(project, "guard"), "approval"))
    if t is None:
        t = _explicit_timeout(_section(_section(user, "guard"), "approval"))
    return TIMEOUT_DEFAULT_S if t is None else t


def _valid_ignore(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return [s.strip() for s in value if isinstance(s, str) and s.strip()]


def _valid_overrides(value: Any) -> Dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {k: v.lower() for k, v in value.items() if isinstance(v, str) and v.lower() in SEVERITY_POINTS}


def _valid_extra_with_warnings(value: Any, existing_count: int = 0) -> Tuple[List[str], List[str]]:
    out: List[str] = []
    warnings: List[str] = []
    if not isinstance(value, list):
        return out, warnings
    max_extra = getattr(_secrets, "MAX_EXTRA_SECRET_PATHS", 20)
    for rx in value:
        if not isinstance(rx, str) or not rx:
            continue
        if existing_count + len(out) >= max_extra:
            warnings.append(f"ignored unsafe/invalid extra_secret_paths pattern: {rx} (limit {max_extra})")
            continue
        if safe_user_regex is not None:
            ok = safe_user_regex(rx) is not None
        else:
            try:
                re.compile(rx)
                ok = len(rx) <= 200
            except re.error:
                ok = False
        if ok:
            out.append(rx)
        else:
            warnings.append(f"ignored unsafe/invalid extra_secret_paths pattern: {rx}")
    return out, warnings


def _valid_extra(value: Any) -> List[str]:
    out, _ = _valid_extra_with_warnings(value)
    return out


def _risk_settings(project: Dict[str, Any], user: Dict[str, Any]) -> Dict[str, Any]:
    """`assess` is what risk.py gets: the user's ignore and severity overrides, the
    union of secret-path patterns, and the user's rules plus the project's (a project may
    add rules, never remove one). The project's ignore and severity overrides come back
    separately, for the tighten-only checks."""
    user_extra, user_extra_warnings = _valid_extra_with_warnings(user.get("extra_secret_paths"))
    proj_extra, proj_extra_warnings = _valid_extra_with_warnings(
        project.get("extra_secret_paths"), existing_count=len(user_extra)
    )
    user_commands, user_command_warnings = _risk.parse_command_rules(
        user.get("command_rules"), f"{USER_CONFIG_DIR}/{USER_CONFIG_FILE}")
    proj_commands, proj_command_warnings = _risk.parse_command_rules(
        project.get("command_rules"), POLICY_FILE)
    user_tools = _risk.parse_tool_rules(user.get("tool_rules"))
    proj_tools = _risk.parse_tool_rules(project.get("tool_rules"))
    return {
        "assess": {
            "ignore": _valid_ignore(user.get("ignore")),
            "severity_overrides": _valid_overrides(user.get("severity_overrides")),
            "extra_secret_paths": user_extra + proj_extra,
            "mcp_verbs": _risk.merge_mcp_verbs(
                _risk.parse_mcp_verbs(user.get("mcp_verbs")),
                _risk.parse_mcp_verbs(project.get("mcp_verbs"), allow_read=False)),
            "tool_rules": {"deny": user_tools["deny"] + proj_tools["deny"],
                           "ask": user_tools["ask"] + proj_tools["ask"]},
            "command_rules": {"deny": user_commands["deny"] + proj_commands["deny"],
                              "ask": user_commands["ask"] + proj_commands["ask"]},
        },
        "project_ignore": _valid_ignore(project.get("ignore")),
        "project_severity": _valid_overrides(project.get("severity_overrides")),
        "extra_warnings": (user_extra_warnings + proj_extra_warnings
                           + user_command_warnings + proj_command_warnings),
    }


def _apply_project_severity(risks: List[Risk], project_severity: Dict[str, str]) -> List[str]:
    """A project may raise a risk's severity. Lowering it is ignored, with a warning."""
    warnings: List[str] = []
    lowered = set()
    for r in risks:
        level = project_severity.get(r.code)
        if level is None:
            for k, v in project_severity.items():
                if _risk._risk_matches_key(r, k):
                    level = v
                    break
        if level is None:
            continue
        if SEVERITY_POINTS[level] > SEVERITY_POINTS[r.severity]:
            r.severity = level
        elif SEVERITY_POINTS[level] < SEVERITY_POINTS[r.severity] and r.code not in lowered:
            lowered.add(r.code)
            warnings.append(f"Ignored severity_overrides for {r.code} in {POLICY_FILE}: "
                            f"a project may raise a risk's severity, not lower it.")
    return warnings


def _ignore_warnings(risks: List[Risk], project_ignore: List[str]) -> List[str]:
    """The project's ignore entries are not applied. Warn for each one that would have hidden a risk."""
    warnings = []
    for entry in project_ignore:
        needle = entry.lower()
        if any(needle in r.code.lower() or needle in r.reason.lower() for r in risks):
            warnings.append(f"Ignored ignore entry \"{entry}\" in {POLICY_FILE}: a project cannot hide risks from the guard.")
    return warnings


def _exclusion_warnings(user_rules: List[Dict[str, Any]], project_rules: List[Dict[str, Any]],
                        risks: List[Risk], list_name: str) -> List[str]:
    """Warn when a project exclusion would have removed a risk that the user or default list matches."""
    warnings: List[str] = []
    seen = set()
    negated = [p for p in project_rules if p["negate"]]
    for r in risks:
        if not _matches_list(user_rules, r):
            continue
        for p in negated:
            if p["raw"] not in seen and _rule_matches(p, r):
                seen.add(p["raw"])
                warnings.append(f"Ignored exclusion \"{p['raw']}\" in the project's {list_name} list: an exclusion "
                                f"cannot remove a rule from the user or default {list_name} list.")
    return warnings


# ---------------------------------------------------------------- agents

def normalize_agent(value: Any) -> Optional[str]:
    """The canonical name of the agent a `--agent` value names, or None for anything else."""
    if not isinstance(value, str):
        return None
    name = value.strip().lower()
    return name if name in AGENTS else None


def infer_agent(event: Any) -> str:
    """The agent a payload came from, read from the payload's own shape. Used only when the
    hook command has no (or an unknown) `--agent`: Codex sends `turn_id`, Droid a
    `permission_mode` of off/spec/auto-*, and everything else is Claude Code."""
    if not isinstance(event, dict):
        return AGENT_CLAUDE
    if "turn_id" in event:
        return AGENT_CODEX
    mode = event.get("permission_mode")
    if isinstance(mode, str) and (mode in ("off", "spec") or mode.startswith("auto-")):
        return AGENT_DROID
    return AGENT_CLAUDE


def canonical_tool(agent: str, tool: str) -> Optional[str]:
    """The canonical name a tool is assessed and logged under, or None for a name this
    agent's vocabulary does not know. Claude Code names are canonical already; a Droid
    connector name gets the `mcp__` prefix the receipts use (the classifier reads both)."""
    if agent == AGENT_CLAUDE:
        return tool
    mapped = (_DROID_TOOLS if agent == AGENT_DROID else _CODEX_TOOLS).get(tool)
    if mapped is not None:
        return mapped
    if mcp_parts(tool) is not None:
        return tool if tool.startswith("mcp__") else "mcp__" + tool
    return None


def _patch_text(tool_input: Any) -> str:
    """The body of an ApplyPatch/apply_patch call. Codex sends it as `command`, Droid as
    `input` or `patch`; a client that sends the text itself is accepted too."""
    if isinstance(tool_input, str):
        return tool_input
    if isinstance(tool_input, dict):
        for key in ("command", "input", "patch"):
            value = tool_input.get(key)
            if isinstance(value, str) and value.strip():
                return value
    return ""


def _patch_steps(text: str) -> List[Tuple[str, Dict[str, Any]]]:
    """One canonical step per file the patch touches (the same parser the Codex and Droid
    adapters use). A body that cannot be parsed becomes one Edit with no path, so its text
    is still scanned for secret content."""
    from .adapters.codex import patch_to_steps
    try:
        steps = patch_to_steps(text)
    except Exception:
        steps = []
    return steps or [("Edit", {"old_string": "", "new_string": text})]


def _droid_steps(tool: str, raw_input: Any) -> List[Tuple[str, Dict[str, Any]]]:
    if tool in _PATCH_TOOLS:
        return _patch_steps(_patch_text(raw_input))
    tool_input = raw_input if isinstance(raw_input, dict) else {}
    if tool == "Edit":
        # Droid names the Edit fields old_str/new_str; the rules read old_string/new_string.
        mapped = dict(tool_input)
        if "old_string" not in mapped and isinstance(mapped.get("old_str"), str):
            mapped["old_string"] = mapped["old_str"]
        if "new_string" not in mapped and isinstance(mapped.get("new_str"), str):
            mapped["new_string"] = mapped["new_str"]
        return [("Edit", mapped)]
    return [(_DROID_TOOLS.get(tool, tool), tool_input)]


def normalize_event(event: dict, agent: str) -> Tuple[str, Dict[str, Any], List[Tuple[str, Dict[str, Any]]]]:
    """(the agent's own tool name, its tool_input, the canonical steps to assess).

    Claude Code names are canonical, so its event is one step unchanged. Droid's `Execute`
    becomes a `Bash`, `Create` a `Write`, `Edit` an `Edit` with `old_string`/`new_string`,
    its searches a `Search`, `FetchUrl` a `WebFetch`, and `ApplyPatch` one step per file.
    Codex's `apply_patch` likewise becomes one step per file. MCP tool names are passed
    through unchanged: the classifier understands `mcp__server__tool` and Droid's bare
    `server__tool` alike. Any other name keeps its own, so the guard never decides on a
    tool it does not understand."""
    tool = str(event.get("tool_name") or "?")
    raw_input = event.get("tool_input")
    tool_input = raw_input if isinstance(raw_input, dict) else {}
    if agent == AGENT_DROID:
        return tool, tool_input, _droid_steps(tool, raw_input)
    if agent == AGENT_CODEX and tool == "apply_patch":
        return tool, tool_input, _patch_steps(_patch_text(raw_input))
    return tool, tool_input, [(tool, tool_input)]


# ---------------------------------------------------------------- decision

def _event_cwd(event: Any) -> str:
    cwd = event.get("cwd") if isinstance(event, dict) else None
    return cwd if isinstance(cwd, str) and cwd.strip() else os.getcwd()


def _summary(tool: str, tool_input: Dict[str, Any]) -> str:
    """What the log and the approver see: the command for shell tools, the path for
    file tools. Write/Edit bodies and MCP arguments are never included."""
    if tool in ("Bash", "PowerShell"):
        text = tool_input.get("command")
    elif tool in ("Write", "Edit", "MultiEdit", "Read", "Delete", "NotebookEdit"):
        text = tool_input.get("file_path") or tool_input.get("path") or tool_input.get("notebook_path")
    else:
        text = None
    return clean(text, 200) if isinstance(text, str) else ""


def _steps_summary(steps: List[Tuple[str, Dict[str, Any]]]) -> str:
    """The summary of a call: the single step's own summary, or the comma list of the paths
    a patch touches. File bodies never appear."""
    if len(steps) == 1:
        return _summary(steps[0][0], steps[0][1])
    return clean(", ".join(s for s in (_summary(t, i) for t, i in steps) if s), 200)


def _reasons_text(risks: List[Risk], limit: int = 300) -> str:
    seen: List[str] = []
    for r in risks:
        t = clean(r.reason, 200)
        if t not in seen:
            seen.append(t)
    return clean("; ".join(seen) or "no details", limit)


def _evaluate(risks: List[Risk], cfg: Dict[str, Any]) -> Tuple[str, List[Risk]]:
    """Most restrictive wins: any deny beats any ask, which beats allow. Each list is
    checked on its own, so a project list cannot remove a match from the user's list."""
    denied = [r for r in risks if any(_matches_list(lst, r) for lst in cfg["deny"])]
    if denied:
        return "deny", denied
    asked = [r for r in risks if any(_matches_list(lst, r) for lst in cfg["ask"])]
    if asked:
        return "ask", asked
    return "allow", []


def _ask_approver(request: Dict[str, Any], cfg: Dict[str, Any], approver: Optional[ApproverFn],
                  drivers: List[Risk], reason: str) -> Tuple[str, str, str]:
    """Send an ask to the approval server. Returns (decision, approval status, reason)."""
    approval_cfg = cfg["approval"]
    call = approver or http_approver
    try:
        status = call(request, approval_cfg)
    except Exception as exc:  # unreachable server, bad key, bad answer: ask locally
        detail = clean(f"{type(exc).__name__}: {exc}", 160)
        return "ask", "unavailable", f"{reason} (approval unavailable: {detail})"
    text = _reasons_text(drivers)
    if status == "approved":
        return "allow", status, f"Approved in RunLedger: {text}"
    if status == "denied":
        return "deny", status, f"Denied in the RunLedger approval queue: {text}"
    if status == "expired":
        return "deny", status, f"Approval expired before a decision: {text}"
    if status == "timeout":
        if approval_cfg["on_timeout"] == "ask":
            return "ask", status, f"{reason} (no approval in time, asking locally)"
        return "deny", status, f"No approval within {approval_cfg['timeout_s']:g}s: {text}"
    return "deny", clean(status, 40), f"Unknown approval status: {text}"


class _DecisionDict(dict):
    """Decision dictionary that also supports attribute access (`d.decision`, `d.codes`, etc.)."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None

    def outcome_when_enforced(self) -> str:
        return str(self.get("policy_decision") or self.get("decision") or "allow")


def decide(event: dict, policy: Optional[dict] = None, approver: Optional[ApproverFn] = None,
           agent: Optional[str] = None) -> _DecisionDict:
    """Decide one PreToolUse event.

    `agent` is the installed hook's agent (`claude-code`, `droid` or `codex`). Without it,
    or with a name the guard does not know, the agent comes from the payload's own shape.

    `policy` is the project's .runledger.json dict. The user's ~/.runledger/config.json
    is loaded here. The effective settings are the user's, tightened by the project's
    (see the module docstring). `approver(request, approval_cfg) -> str` returns
    "approved", "denied", "expired" or "timeout". The default is the HTTP client
    (`http_approver`). It is used only when the decision is "ask" and an approval server
    is configured, or when an approver is passed in.

    Returns a dict with: decision (allow|deny|ask, after monitor mode, the approval and the
    agent's own rules), policy_decision (before them), mode, agent, tool, canonical_tool,
    tool_use_id, session_id, cwd, reason, codes, risks [{severity, code, reason}], approval
    (None or the server status), summary, and warnings (project settings that were ignored).
    Nothing here touches the log or stdout."""
    event = event if isinstance(event, dict) else {}
    agent = normalize_agent(agent) or infer_agent(event)
    event_cwd = _event_cwd(event)
    cwd = project_root(event)
    project = policy if isinstance(policy, dict) else read_policy(cwd, _resolved=True)
    user = load_user_config()
    cfg, warnings = _effective_guard(project, user, trusted_approval_settings(user))
    warnings = _project_key_warnings(project) + warnings

    tool, tool_input, steps = normalize_event(event, agent)
    session_id = str(event.get("session_id") or "")
    tool_use_id = str(event.get("tool_use_id") or "")

    rs = _risk_settings(project, user)
    warnings += rs.get("extra_warnings", [])
    risks: List[Risk] = []
    for index, (step_tool, step_input) in enumerate(steps, start=1):
        step = Step(index=index, tool=step_tool, input=step_input,
                    tool_use_id=tool_use_id, model=None, timestamp=None)
        risks.extend(assess_step(step, cwd, rs["assess"], workdir=event_cwd))
    warnings += _apply_project_severity(risks, rs["project_severity"])
    warnings += _ignore_warnings(risks, rs["project_ignore"])
    warnings += _exclusion_warnings(cfg["user_deny"], cfg["project_deny"], risks, "deny")
    warnings += _exclusion_warnings(cfg["user_ask"], cfg["project_ask"], risks, "ask")

    policy_decision, drivers = _evaluate(risks, cfg)
    summary = _steps_summary(steps)
    # Everything decided above used the shell's own words; the label is the whole spaced drive
    # path in the reason text this decision shows. The label names what the command said, so it
    # goes on before the redaction and the truncation of that output copy, never after them: a
    # risk list that leaks here also reaches the approval server (see `_ask_approver`).
    shown = labels.command_labels(tool, tool_input.get("command"))
    risks_out = [{"severity": r.severity, "code": r.code,
                  "reason": clean(labels.show_reason(r.reason, shown), 300)} for r in risks]

    final = policy_decision
    approval: Optional[str] = None
    if policy_decision == "deny":
        reason = f"Blocked by RunLedger guard: {_reasons_text(drivers)}"
    elif policy_decision == "ask":
        reason = f"RunLedger guard needs confirmation: {_reasons_text(drivers)}"
    else:
        reason = "No deny or ask rule matched."

    if cfg["mode"] == "monitor":
        final = "allow"
        if policy_decision != "allow":
            reason = f"Monitor mode, not enforced. {reason}"
    elif policy_decision == "ask" and (approver is not None or cfg["approval"]["server"]):
        request = {"session_id": session_id, "tool": tool, "summary": summary,
                   "risks": risks_out, "cwd": cwd}
        final, approval, reason = _ask_approver(request, cfg, approver, drivers, reason)
    if agent == AGENT_CODEX and policy_decision == "ask" and final != "allow":
        final, reason = _codex_ask_outcome(cfg, drivers, approval, reason)

    return _DecisionDict({
        "decision": final,
        "policy_decision": policy_decision,
        "mode": cfg["mode"],
        "agent": agent,
        "tool": tool,
        "canonical_tool": canonical_tool(agent, tool),
        "tool_use_id": tool_use_id,
        "session_id": session_id,
        "cwd": cwd,
        "reason": clean(labels.show_reason(reason, shown), 500),
        "codes": sorted({r.code for r in risks}),
        "risks": risks_out,
        "approval": approval,
        "summary": summary,
        "warnings": list(dict.fromkeys(warnings)),
    })


def _codex_ask_outcome(cfg: Dict[str, Any], drivers: List[Risk], approval: Optional[str],
                       reason: str) -> Tuple[str, str]:
    """Codex cannot prompt from a hook and would silently continue on an ask, so an ask
    becomes a deny there that names the rule and the way forward. The user's own file can
    choose to allow it instead (`"codex_ask": "allow"`); an approval answer from the server
    always wins over that, and its own wording is kept in the reason."""
    text = _reasons_text(drivers)
    if cfg.get("codex_ask") == "allow" and approval is None:
        return "allow", (f"RunLedger guard needs confirmation ({text}) but Codex cannot prompt from a hook; "
                         f"allowed because guard.codex_ask is allow in {USER_CONFIG_DIR}/{USER_CONFIG_FILE}.")
    if approval is not None:
        # The approval server's own answer is the story; add what Codex can and cannot do about it.
        return "deny", (f"{reason} Codex cannot prompt from a hook, so the call is denied: approve it in the "
                        f"RunLedger approval server or adjust the rules, then re-run.")
    return "deny", (f"RunLedger guard needs confirmation but Codex cannot prompt from a hook, so this call is "
                    f"denied: {text}. Approve it in the RunLedger approval server or adjust the rules, "
                    f"then re-run.")


def evaluate(event: dict, policy: Optional[dict] = None, approver: Optional[ApproverFn] = None,
             agent: Optional[str] = None) -> _DecisionDict:
    """Evaluate a PreToolUse event and write warnings + the decision record to `.runledger/guard.log`."""
    decision = decide(event, policy=policy, approver=approver, agent=agent)
    _log_quietly(lambda: _log_warnings_and_decision(decision))
    return decision


def hook_output(decision: dict, agent: Optional[str] = None) -> Optional[dict]:
    """The JSON the agent expects on stdout, or None for "print nothing".

    Claude Code and Droid read the same `hookSpecificOutput.permissionDecision` object and
    both support "ask". Codex only blocks on "deny" (an ask would let the call run), so it
    never gets an ask here: a deny prints the same JSON object. A plain allow prints
    nothing for every agent, so the agent's own permission rules still apply: the guard is
    meant to restrict, not to grant."""
    if agent is None:
        agent = decision.get("agent")
    agent = normalize_agent(agent) or AGENT_CLAUDE
    final = decision.get("decision")
    if agent == AGENT_CODEX:
        return _deny_output(str(decision.get("reason") or "")) if final == "deny" else None
    if final == "allow" and decision.get("approval") != "approved":
        return None  # no opinion: the agent's own permission rules decide
    if final not in ("allow", "deny", "ask"):
        return None
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": final,
        "permissionDecisionReason": decision.get("reason") or "",
    }}


def _deny_output(reason: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                   "permissionDecision": "deny",
                                   "permissionDecisionReason": clean(reason, 500)}}


# ---------------------------------------------------------------- approval client

def _http_json(method: str, url: str, key: str, body: Optional[dict], timeout: float) -> Tuple[int, dict]:
    """One request. HTTP status errors come back as (status, {}); transport
    errors raise ApprovalError. The key is never put into an error message."""
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            code = resp.status
            raw = resp.read(_MAX_RESPONSE_BYTES)
    except urllib.error.HTTPError as exc:
        return exc.code, {}
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise ApprovalError(f"cannot reach the approval server ({type(exc).__name__})") from None
    try:
        parsed = json.loads(raw.decode("utf-8") or "{}")
    except ValueError:
        parsed = {}
    return code, parsed if isinstance(parsed, dict) else {}


def http_approver(request: Dict[str, Any], approval: Dict[str, Any]) -> str:
    """Default approver. POST {server}/api/approvals (expects 201 {"id","status"}),
    then GET {server}/api/approvals/{id} every POLL_INTERVAL_S until the status is
    approved, denied or expired, or until timeout_s has passed ("timeout")."""
    server = str(approval.get("server") or "").rstrip("/")
    if not server.lower().startswith(("http://", "https://")):
        raise ApprovalError("approval server must be an http:// or https:// URL")
    env_name = approval.get("api_key_env") or KEY_ENV_DEFAULT
    key = os.environ.get(env_name, "")
    if not key:
        raise ApprovalError(f"environment variable {env_name} is not set")
    timeout_s = float(approval.get("timeout_s") or TIMEOUT_DEFAULT_S)
    deadline = time.monotonic() + timeout_s

    code, data = _http_json("POST", f"{server}/api/approvals", key, request, HTTP_TIMEOUT_S)
    if code != 201:
        raise ApprovalError(f"approval server answered HTTP {code} to the request")
    appr_id = data.get("id")
    if not isinstance(appr_id, (str, int)) or appr_id == "":
        raise ApprovalError("approval server returned no request id")
    url = f"{server}/api/approvals/{urllib.parse.quote(str(appr_id), safe='')}"

    errors = 0
    while True:
        try:
            code, data = _http_json("GET", url, key, None, HTTP_TIMEOUT_S)
        except ApprovalError:
            errors += 1
            if errors >= MAX_POLL_ERRORS:
                raise
        else:
            if code != 200:
                raise ApprovalError(f"approval server answered HTTP {code} while polling")
            errors = 0
            status = data.get("status")
            if status in ("approved", "denied", "expired"):
                return status
        if time.monotonic() >= deadline:
            return "timeout"
        time.sleep(max(0.0, min(POLL_INTERVAL_S, deadline - time.monotonic())))


# ---------------------------------------------------------------- log

def _append_logs(cwd: str, records: List[Dict[str, Any]]) -> None:
    if not cwd or not records or _paths.is_unc(cwd):
        return
    path = Path(cwd) / LOG_DIR / LOG_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    lines: List[str] = []
    for fields in records:
        rec: Dict[str, Any] = {"ts": ts}
        for k, v in fields.items():
            rec[k] = clean(v, 200) if isinstance(v, str) else v
        lines.append(json.dumps(rec, ensure_ascii=False) + "\n")
    with path.open("a", encoding="utf-8") as fh:
        fh.writelines(lines)


def _append_log(cwd: str, fields: Dict[str, Any]) -> None:
    _append_logs(cwd, [fields])


def _decision_record(decision: dict) -> Dict[str, Any]:
    return {
        "session_id": decision["session_id"], "tool": decision["tool"],
        "agent": decision.get("agent"), "canonical_tool": decision.get("canonical_tool"),
        "mode": decision["mode"],
        "tool_use_id": decision.get("tool_use_id", ""),
        "decision": decision["decision"], "policy_decision": decision["policy_decision"],
        "codes": decision["codes"], "approval": decision["approval"],
        "summary": decision["summary"], "reason": decision["reason"],
    }


def _warning_records(decision: dict) -> List[Dict[str, Any]]:
    return [
        {"session_id": decision["session_id"], "decision": "warning", "reason": text}
        for text in (decision.get("warnings") or [])
    ]


def _log_decision(decision: dict) -> None:
    _append_log(decision["cwd"], _decision_record(decision))


def _log_warnings(decision: dict) -> None:
    _append_logs(decision["cwd"], _warning_records(decision))


def _log_warnings_and_decision(decision: dict) -> None:
    _append_logs(decision["cwd"], _warning_records(decision) + [_decision_record(decision)])


def _log_quietly(fn: Callable[[], None]) -> None:
    try:
        fn()
    except Exception:  # a log that cannot be written must not change the decision
        pass


# ---------------------------------------------------------------- hook entry point

def handle(raw: str, agent: Optional[str] = None) -> Optional[dict]:
    """Hook input (JSON text) to hook output dict, or None to print nothing.
    Never raises. `agent` is the installed hook's `--agent` value; without it, or with a
    value the guard does not know, the agent comes from the payload's own shape."""
    event: Any = None
    policy: Dict[str, Any] = {}
    resolved = normalize_agent(agent)
    try:
        event = json.loads(raw)
        if not isinstance(event, dict):
            raise ValueError("hook input is not a JSON object")
        if not event:
            return None
        if resolved is None:
            resolved = infer_agent(event)
        policy = read_policy(project_root(event), _resolved=True)
        decision = decide(event, policy, agent=resolved)
    except Exception as exc:
        return _on_error(event, policy, exc, agent=resolved)
    _log_quietly(lambda: _log_warnings_and_decision(decision))
    return hook_output(decision, resolved)


def _on_error(event: Any, policy: Dict[str, Any], exc: BaseException,
              agent: Optional[str] = None) -> Optional[dict]:
    try:
        cwd = project_root(event)
        if not policy:
            policy = read_policy(cwd)
        user = load_user_config()
        cfg, _ = _effective_guard(policy, user, trusted_approval_settings(user))
    except Exception:
        return None  # cannot even read the settings: fail open
    msg = clean(f"{type(exc).__name__}: {exc}", 200)
    if isinstance(event, dict):
        tool = str(event.get("tool_name") or "?")
        session = str(event.get("session_id") or "")
        resolved = agent or normalize_agent(infer_agent(event))
        _log_quietly(lambda: _append_log(cwd, {
            "session_id": session, "tool": tool, "agent": resolved, "mode": cfg["mode"],
            "decision": "error", "tool_use_id": str(event.get("tool_use_id") or ""),
            "codes": [], "reason": msg}))
    if cfg["mode"] == "monitor":
        return None
    if cfg["fail_closed"]:
        return _deny_output(f"RunLedger guard could not evaluate this call (fail_closed is on): {msg}")
    return None


def main(agent: Optional[str] = None) -> int:
    """Entry point for `runledger guard` as a hook: stdin JSON in, stdout JSON out.
    `agent` is the installed hook's `--agent` value; an unknown one is reported on stderr
    and never blocks, because a blocking argument error would stop every tool call. When
    stdin is an interactive terminal (TTY), print the guard help and exit 2."""
    try:
        if sys.stdin is not None and sys.stdin.isatty():
            from .cli import print_guard_help
            print_guard_help()
            return 2
    except Exception:
        pass
    resolved = normalize_agent(agent)
    if agent and resolved is None:
        _warn(f"unknown --agent {agent!r}: reading the agent from the payload instead. "
              f"Known agents: {', '.join(AGENTS)}.")
    try:
        raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
    except Exception:
        raw = ""
    try:
        out = handle(raw, resolved)
    except Exception:
        out = None
    if out is not None:
        try:
            sys.stdout.write(json.dumps(out) + "\n")
            sys.stdout.flush()
        except Exception:
            pass
    return 0


def _warn(message: str) -> None:
    """A note for the human reading the hook's stderr. Never raises."""
    try:
        sys.stderr.write(f"runledger guard: {message}\n")
        sys.stderr.flush()
    except Exception:
        pass


# ---------------------------------------------------------------- CLI helpers

def cmd_test(event_json: str, agent: Optional[str] = None) -> int:
    """`runledger guard test '<json event>' [--agent X]`: print the decision and the hook
    output for that agent. Writes no log."""
    try:
        event = json.loads(event_json)
    except ValueError as exc:
        print(f"error: event is not valid JSON: {exc}", file=sys.stderr)
        return 1
    if not isinstance(event, dict):
        print("error: event must be a JSON object", file=sys.stderr)
        return 1
    resolved = normalize_agent(agent)
    try:
        decision = decide(event, read_policy(project_root(event)), agent=resolved)
    except Exception as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"decision": decision, "hook_output": hook_output(decision, resolved)}, indent=2))
    return 0


def settings_path(project: Optional[str] = None, global_scope: bool = False,
                  home: Optional[Path] = None) -> Path:
    """The Claude Code settings file this install writes: `~/.claude/settings.json` for a
    global install, else `<project>/.claude/settings.json`. Without `--project` the folder
    is the project root (nearest ancestor with `.runledger.json` or `.git`), not the cwd."""
    if global_scope:
        return (Path(home) if home is not None else Path.home()) / ".claude" / "settings.json"
    base = Path(project) if project else Path(project_root())
    return base / ".claude" / "settings.json"


def hook_timeout_for(approval_timeout_s: Optional[float] = None) -> int:
    """The hook's "timeout" in seconds: the approval wait plus HOOK_MARGIN_S, rounded up.
    Claude Code stops a hook at its timeout and then lets the tool run, so the hook must
    outlive the approval wait. None means the default approval wait (120 s, so 135)."""
    base = TIMEOUT_DEFAULT_S if approval_timeout_s is None else float(approval_timeout_s)
    return int(math.ceil(base)) + HOOK_MARGIN_S


def guard_hook_command(portable: bool = False) -> str:
    """The hook command written to settings.json: `"<sys.executable>" -m runledger guard`
    by default, or bare `runledger guard` when `portable=True`."""
    return HOOK_COMMAND if portable else f'"{sys.executable}" -m runledger guard'


def checkpoint_hook_command(portable: bool = False) -> str:
    """The checkpoint hook command written to settings.json: `"<sys.executable>" -m runledger checkpoint --hook`
    by default, or bare `runledger checkpoint --hook` when `portable=True`."""
    from .rollback import HOOK_COMMAND as checkpoint_command
    return checkpoint_command if portable else f'"{sys.executable}" -m runledger checkpoint --hook'


def hook_entry(hook_seconds: int, portable: bool = False) -> Dict[str, Any]:
    return {"matcher": "*", "hooks": [{"type": "command", "command": guard_hook_command(portable=portable), "timeout": hook_seconds}]}


def _owned_command_parts(command: Any) -> Optional[List[str]]:
    """The words of a complete RunLedger guard hook command, else None.

    Only the forms the installer writes count: bare `runledger guard` (optionally with a
    path or `.exe`) and `"<interpreter>" -m runledger guard`, for every interpreter it can write
    (`sys.executable` itself, whatever the file is called, plus `python3.13t.exe` and the other
    interpreter names), each optionally followed by `--agent <agent>`. A foreign hook that
    merely mentions the words (`echo runledger guard`, `other.sh && runledger guard`, a
    pipeline, a redirection, a command with an attached shell operator) is not ours and must
    never be replaced or removed."""
    tails = [("guard",)] + [("guard", "--agent", agent) for agent in AGENTS]
    for tail in tails:
        parts = _paths.owned_hook_command(command, tail)
        if parts is not None:
            return parts
    return None


def _is_guard_hook(h: Any) -> bool:
    return isinstance(h, dict) and _owned_command_parts(h.get("command")) is not None


def _group_has_guard(group: Any) -> bool:
    if not isinstance(group, dict):
        return False
    hooks = group.get("hooks")
    return any(_is_guard_hook(h) for h in (hooks if isinstance(hooks, list) else []))


def install_hook(path: Path, approval_timeout_s: Optional[float] = None,
                 portable: bool = False) -> Tuple[bool, Optional[Path]]:
    """Add the guard and lifecycle checkpoint entries, keeping everything else.

    The entry's "timeout" is hook_timeout_for(approval_timeout_s). An existing guard
    hook (any matcher) gets its command and timeout updated in place; a second entry is
    never added.

    Returns (changed, backup). The original is copied to `settings.json.bak` before
    any change; a file that is already up to date is not written. Raises InstallError
    (and writes nothing) if the file is not a JSON object."""
    path = Path(path)
    want = hook_timeout_for(approval_timeout_s)
    want_cmd = guard_hook_command(portable=portable)
    want_cp_cmd = checkpoint_hook_command(portable=portable)
    existed, settings = _read_settings(path)
    hooks = settings.get("hooks")
    hooks = {} if hooks is None else hooks
    if not isinstance(hooks, dict):
        raise InstallError(f"'hooks' in {path} must be an object. Nothing was changed.")
    pre = hooks.get("PreToolUse")
    pre = [] if pre is None else pre
    if not isinstance(pre, list):
        raise InstallError(f"'hooks.PreToolUse' in {path} must be a list. Nothing was changed.")

    changed = False
    found = False
    for group in pre:
        if not _group_has_guard(group):
            continue
        found = True
        for h in group["hooks"]:
            if _is_guard_hook(h):
                if h.get("command") != want_cmd:
                    h["command"] = want_cmd
                    changed = True
                if h.get("timeout") != want:
                    h["timeout"] = want
                    changed = True
    if not found:
        pre.append(hook_entry(want, portable=portable))
        changed = True
    from .rollback import HOOK_EVENTS, is_checkpoint_hook
    for event in HOOK_EVENTS:
        groups = hooks.get(event)
        groups = [] if groups is None else groups
        if not isinstance(groups, list):
            raise InstallError(f"'hooks.{event}' in {path} must be a list. Nothing was changed.")
        found_checkpoint = False
        for group in groups:
            entries = group.get("hooks") if isinstance(group, dict) else None
            for h in entries if isinstance(entries, list) else []:
                if is_checkpoint_hook(h):
                    if group.get("matcher") in (None, "", "*"):
                        found_checkpoint = True
                        if h.get("command") != want_cp_cmd:
                            h["command"] = want_cp_cmd
                            changed = True
                    if h.get("timeout") != 120:
                        h["timeout"] = 120
                        changed = True
        if not found_checkpoint:
            groups.append({"hooks": [{"type": "command", "command": want_cp_cmd, "timeout": 120}]})
            changed = True
        hooks[event] = groups
    if not changed:
        return False, None

    hooks["PreToolUse"] = pre
    settings["hooks"] = hooks
    return True, _write_settings(path, settings, existed)


def uninstall_hook(path: Path) -> Tuple[bool, Optional[Path]]:
    """Remove guard and checkpoint hooks from settings.json, keeping everything else.

    Other hooks in the same PreToolUse group stay. A group left with no hooks is removed,
    then an empty PreToolUse list and an empty hooks object. Returns (changed, backup);
    the original is copied to `settings.json.bak` first, and a file without the hook is
    not written. Raises InstallError (and writes nothing) if the file is not a JSON object."""
    path = Path(path)
    existed, settings = _read_settings(path)
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return False, None
    from .rollback import HOOK_EVENTS, is_checkpoint_hook
    removed = False
    for event in ("PreToolUse", *HOOK_EVENTS):
        groups = hooks.get(event)
        if not isinstance(groups, list):
            continue
        kept: List[Any] = []
        for group in groups:
            entries = group.get("hooks") if isinstance(group, dict) else None
            if not isinstance(entries, list):
                kept.append(group)
                continue
            rest = [h for h in entries if not (
                _is_guard_hook(h) if event == "PreToolUse" else is_checkpoint_hook(h))]
            if len(rest) == len(entries):
                kept.append(group)
            else:
                removed = True
                if rest:
                    kept.append(dict(group, hooks=rest))
        if kept:
            hooks[event] = kept
        elif groups:
            del hooks[event]
    if not removed:
        return False, None
    if not hooks:
        del settings["hooks"]
    return True, _write_settings(path, settings, existed)


def _read_settings(path: Path) -> Tuple[bool, Dict[str, Any]]:
    """(existed, settings). A missing or empty file gives {}. Raises InstallError if the
    file is not a JSON object."""
    if not path.exists():
        return False, {}
    text = path.read_text(encoding="utf-8-sig")
    if not text.strip():
        return True, {}
    try:
        settings = json.loads(text)
    except ValueError as exc:
        raise InstallError(f"{path} is not valid JSON ({exc}); fix or move it. Nothing was changed.") from None
    if not isinstance(settings, dict):
        raise InstallError(f"{path} must contain a JSON object. Nothing was changed.")
    return True, settings


def _write_settings(path: Path, settings: Dict[str, Any], existed: bool) -> Optional[Path]:
    """Write atomically, after copying an existing file to `settings.json.bak`. Returns the backup."""
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if existed:
        backup = path.with_name(path.name + ".bak")
        shutil.copy2(path, backup)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return backup


def verify_hook_command(command: str, cwd: Optional[str] = None) -> Tuple[bool, str]:
    """Run the configured hook command once (`{}` on stdin, timeout 15 s) the way the agent
    will: the stored command itself, in `cwd` (the project folder), with the environment as
    it is. Nothing is added to PYTHONPATH, so a command that cannot import RunLedger there
    is reported instead of passing on the installer's own checkout.
    Returns `(ok, message)`."""
    parts = _owned_command_parts(command)
    if parts is None:
        return False, f"Warning: configured hook command `{command}` is not a RunLedger guard command."
    if len(parts) == 2:
        exe = shutil.which(parts[0])
        if not exe:
            return False, (
                f"Warning: configured hook command `{command}` could not be run "
                "(runledger was not found on PATH). Re-run `runledger guard install` "
                "without `--portable` or add runledger to PATH."
            )
        argv = [exe, parts[1]]
    else:
        argv = list(parts)
    try:
        proc = subprocess.run(
            argv,
            input=b"{}",
            capture_output=True,
            timeout=15,
            cwd=cwd,
        )
    except Exception as exc:
        return False, f"Warning: configured hook command `{command}` could not be run ({exc})."
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or b"").decode("utf-8", errors="replace").strip()
        extra = f": {detail}" if detail else ""
        return False, f"Warning: configured hook command `{command}` exited with code {proc.returncode}{extra}"
    return True, f"Verified hook command: {command} (exit 0)"


def cmd_install(project: Optional[str] = None, global_scope: bool = False,
                home: Optional[Path] = None, portable: bool = False) -> int:
    """Install or update the hook. The hook timeout follows the approval wait: the project's
    timeout_s for a project install, the user's or the default for a global install. Without
    `--project` the settings file is written at the project root, so installing from a
    subfolder configures the whole project."""
    project_dir = None if global_scope else (Path(project) if project else Path(project_root()))
    target = settings_path(project, global_scope, home)
    project_policy = {} if global_scope else read_policy(str(project_dir))
    timeout_s = approval_timeout(project_policy)
    try:
        changed, backup = install_hook(target, timeout_s, portable=portable)
    except InstallError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: cannot update {target}: {exc}", file=sys.stderr)
        return 1
    hook_s = hook_timeout_for(timeout_s)
    cmd = guard_hook_command(portable=portable)
    if changed:
        print(f"Installed or updated the RunLedger guard hook and checkpoint hooks in {target} (hook timeout {hook_s} s)")
        if backup is not None:
            print(f"Backup of the previous file: {backup}")
    else:
        print(f"The RunLedger guard hook and checkpoint hooks in {target} are already up to date (hook timeout {hook_s} s). Nothing changed.")
    verify_cwd = str(Path(home) if home is not None else Path.home()) if global_scope else str(project_dir)
    ok, verify_msg = verify_hook_command(cmd, cwd=verify_cwd)
    print(verify_msg)
    return 0


def cmd_uninstall(project: Optional[str] = None, global_scope: bool = False,
                  home: Optional[Path] = None) -> int:
    """Remove the hook from the project's or the user's Claude Code settings. Without
    `--project` the settings file is the project root's, exactly as for `install`."""
    target = settings_path(project, global_scope, home)
    try:
        changed, backup = uninstall_hook(target)
    except InstallError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: cannot update {target}: {exc}", file=sys.stderr)
        return 1
    if not changed:
        print(f"No RunLedger guard or checkpoint hooks in {target}. Nothing changed.")
        return 0
    print(f"Removed the RunLedger guard hook and checkpoint hooks from {target}")
    if backup is not None:
        print(f"Backup of the previous file: {backup}")
    print("Policy files (.runledger.json, ~/.runledger/config.json) and .runledger/guard.log were left as they are.")
    return 0
