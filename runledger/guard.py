"""Real-time policy guard: a Claude Code PreToolUse hook.

Claude Code runs `runledger guard` before each tool call and writes the call to
stdin as JSON. The guard scores the call with the rules in risk.py, decides
allow / ask / deny, can ask a team approval server, and appends each decision to
<cwd>/.runledger/guard.log.

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
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import risk as _risk
from .parser import Step
from .risk import POLICY_FILE, SEVERITY_POINTS, Risk, assess_step

HOOK_COMMAND = "runledger guard"
HOOK_MARGIN_S = 15             # the hook entry allows the approval wait plus this many seconds
_GUARD_CMD_RE = re.compile(r"\brunledger(?:\.exe)?\"?\s+guard\b|-m\s+runledger\s+guard\b", re.I)

LOG_DIR = ".runledger"
LOG_FILE = "guard.log"
KEY_ENV_DEFAULT = "RUNLEDGER_API_KEY"
SERVER_ENV = "RUNLEDGER_SERVER"
USER_CONFIG_DIR = ".runledger"
USER_CONFIG_FILE = "config.json"
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
    re.compile(r"(?<![A-Za-z0-9])AKIA[0-9A-Z]{16}(?![A-Za-z0-9])"),
    re.compile(r"(?<![A-Za-z0-9])gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"(?<![A-Za-z0-9])xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(r"(?<![A-Za-z0-9])[rs]k_(?:live|test)_[A-Za-z0-9]{16,}"),
    re.compile(r"(?<![A-Za-z0-9])AIza[0-9A-Za-z_-]{35}"),
    re.compile(r"(?<![A-Za-z0-9])glpat-[A-Za-z0-9_-]{20,}"),
    re.compile(r"(?<![A-Za-z0-9])npm_[A-Za-z0-9]{36}"),
    re.compile(r"(?<![A-Za-z0-9])hf_[A-Za-z0-9]{30,}"),
    re.compile(r"(?<![A-Za-z0-9_])rl_[A-Za-z0-9_-]{30,}"),
    re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
]
_FALLBACK_GENERIC_RE = re.compile(
    r"(api[_-]?key|secret|token|password)[\"']?\s*[:=]\s*[\"']([^\"'\s]{8,})[\"']", re.I)

# Unquoted values, masked last. Only the named group "v" is replaced.
_SECRET_NAME = (r"(?:api[_-]?key|apikey|secret|token|passw(?:or)?d|credentials?"
                r"|private[_-]?key|access[_-]?key)")
_VALUE_SECRET_RES = [
    # TOKEN=abc123, export DB_PASSWORD=..., ?access_token=..., --password=...
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
    """Patterns from risk.py when they are there, plus the fallbacks above."""
    try:
        from_risk = [rx for rx, _kind in getattr(_risk, "_CONTENT_SECRET_RULES", []) if hasattr(rx, "sub")]
    except (TypeError, ValueError):
        from_risk = []
    return from_risk + _FALLBACK_SECRET_RES


def redact(text: Any) -> str:
    """Mask secret values. Only the value is replaced, never the surrounding text.
    If redaction itself fails, the whole text is replaced."""
    s = "" if text is None else str(text)
    try:
        generic = getattr(_risk, "_GENERIC_SECRET_RE", None)
        for rx in ([generic] if generic is not None else []) + [_FALLBACK_GENERIC_RE]:
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


def read_policy(cwd: str) -> Dict[str, Any]:
    """The project's `<cwd>/.runledger.json` as a dict. Missing or invalid gives {}."""
    try:
        data = json.loads((Path(cwd) / POLICY_FILE).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError, RecursionError):
        return {}
    return data if isinstance(data, dict) else {}


def user_config_path() -> Path:
    """`~/.runledger/config.json`. The home folder is HOME, then USERPROFILE, then the platform default."""
    base = os.environ.get("HOME") or os.environ.get("USERPROFILE")
    return (Path(base) if base else Path.home()) / USER_CONFIG_DIR / USER_CONFIG_FILE


def load_user_config() -> Dict[str, Any]:
    """The user's config file as a dict, or {} when missing or invalid. Never raises."""
    try:
        data = json.loads(user_config_path().read_text(encoding="utf-8-sig"))
    except Exception:
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
    return [
        f"Ignored approval.{key} in {POLICY_FILE}: a project file cannot set the approval server or key. "
        f"Use {SERVER_ENV} / {KEY_ENV_DEFAULT} or {USER_CONFIG_DIR}/{USER_CONFIG_FILE} in your home folder."
        for key in PROJECT_APPROVAL_KEYS if key in approval
    ]


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


def _rule_matches(rule: Dict[str, Any], risk: Risk) -> bool:
    kind = rule["kind"]
    if kind == "severity":
        return risk.severity == rule["value"]
    if kind == "reason":
        return rule["value"] in risk.reason.lower()
    if kind == "code":
        return risk.code.lower() == rule["value"]
    return risk.code.lower() == rule["code"] and risk.severity == rule["value"]


def _matches_list(rules: List[Dict[str, Any]], risk: Risk) -> bool:
    """True when some positive entry matches and no `!` exclusion in the same list matches."""
    if not any(_rule_matches(r, risk) for r in rules if not r["negate"]):
        return False
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
    user_deny = _parse_rules(u.get("deny"), DEFAULT_DENY)
    user_ask = _parse_rules(u.get("ask"), DEFAULT_ASK)
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

    cfg = {
        "mode": mode,
        "fail_closed": u.get("fail_closed") is True or p.get("fail_closed") is True,
        "deny": [user_deny, project_deny],
        "ask": [user_ask, project_ask],
        "user_deny": user_deny,
        "project_deny": project_deny,
        "user_ask": user_ask,
        "project_ask": project_ask,
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


def _valid_extra(value: Any) -> List[str]:
    out: List[str] = []
    if isinstance(value, list):
        for rx in value:
            if isinstance(rx, str) and rx:
                try:
                    re.compile(rx)
                    out.append(rx)
                except re.error:
                    pass
    return out


def _risk_settings(project: Dict[str, Any], user: Dict[str, Any]) -> Dict[str, Any]:
    """`assess` is what risk.py gets: the user's ignore and severity overrides, and the
    union of secret-path patterns. The project's ignore and severity overrides come
    back separately, for the tighten-only checks."""
    return {
        "assess": {
            "ignore": _valid_ignore(user.get("ignore")),
            "severity_overrides": _valid_overrides(user.get("severity_overrides")),
            "extra_secret_paths": _valid_extra(user.get("extra_secret_paths")) + _valid_extra(project.get("extra_secret_paths")),
        },
        "project_ignore": _valid_ignore(project.get("ignore")),
        "project_severity": _valid_overrides(project.get("severity_overrides")),
    }


def _apply_project_severity(risks: List[Risk], project_severity: Dict[str, str]) -> List[str]:
    """A project may raise a risk's severity. Lowering it is ignored, with a warning."""
    warnings: List[str] = []
    lowered = set()
    for r in risks:
        level = project_severity.get(r.code)
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


# ---------------------------------------------------------------- decision

def _event_cwd(event: Any) -> str:
    cwd = event.get("cwd") if isinstance(event, dict) else None
    return cwd if isinstance(cwd, str) and cwd.strip() else os.getcwd()


def _summary(tool: str, tool_input: Dict[str, Any]) -> str:
    """What the log and the approver see: the command for shell tools, the path for
    file tools. Write/Edit bodies and MCP arguments are never included."""
    if tool in ("Bash", "PowerShell"):
        text = tool_input.get("command")
    elif tool in ("Write", "Edit", "MultiEdit", "Read", "NotebookEdit"):
        text = tool_input.get("file_path") or tool_input.get("path") or tool_input.get("notebook_path")
    else:
        text = None
    return clean(text, 200) if isinstance(text, str) else ""


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


def decide(event: dict, policy: dict, approver: Optional[ApproverFn] = None) -> dict:
    """Decide one PreToolUse event.

    `policy` is the project's .runledger.json dict. The user's ~/.runledger/config.json
    is loaded here. The effective settings are the user's, tightened by the project's
    (see the module docstring). `approver(request, approval_cfg) -> str` returns
    "approved", "denied", "expired" or "timeout". The default is the HTTP client
    (`http_approver`). It is used only when the decision is "ask" and an approval server
    is configured, or when an approver is passed in.

    Returns a dict with: decision (allow|deny|ask, after monitor mode and approval),
    policy_decision (before them), mode, tool, session_id, cwd, reason, codes,
    risks [{severity, code, reason}], approval (None or the server status), summary,
    and warnings (project settings that were ignored). Nothing here touches the log
    or stdout."""
    event = event if isinstance(event, dict) else {}
    project = policy if isinstance(policy, dict) else {}
    user = load_user_config()
    cfg, warnings = _effective_guard(project, user, trusted_approval_settings(user))
    warnings = _project_key_warnings(project) + warnings

    tool = str(event.get("tool_name") or "?")
    raw_input = event.get("tool_input")
    tool_input = raw_input if isinstance(raw_input, dict) else {}
    cwd = _event_cwd(event)
    session_id = str(event.get("session_id") or "")

    rs = _risk_settings(project, user)
    step = Step(index=1, tool=tool, input=tool_input,
                tool_use_id=str(event.get("tool_use_id") or ""), model=None, timestamp=None)
    risks = assess_step(step, cwd, rs["assess"])
    warnings += _apply_project_severity(risks, rs["project_severity"])
    warnings += _ignore_warnings(risks, rs["project_ignore"])
    warnings += _exclusion_warnings(cfg["user_deny"], cfg["project_deny"], risks, "deny")
    warnings += _exclusion_warnings(cfg["user_ask"], cfg["project_ask"], risks, "ask")

    policy_decision, drivers = _evaluate(risks, cfg)
    summary = _summary(tool, tool_input)
    risks_out = [{"severity": r.severity, "code": r.code, "reason": clean(r.reason, 300)} for r in risks]

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

    return {
        "decision": final,
        "policy_decision": policy_decision,
        "mode": cfg["mode"],
        "tool": tool,
        "session_id": session_id,
        "cwd": cwd,
        "reason": clean(reason, 500),
        "codes": sorted({r.code for r in risks}),
        "risks": risks_out,
        "approval": approval,
        "summary": summary,
        "warnings": list(dict.fromkeys(warnings)),
    }


def hook_output(decision: dict) -> Optional[dict]:
    """The JSON Claude Code expects on stdout, or None for "print nothing"."""
    final = decision.get("decision")
    if final == "allow" and decision.get("approval") != "approved":
        return None  # no opinion: Claude Code's own permission rules decide
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

def _append_log(cwd: str, fields: Dict[str, Any]) -> None:
    path = Path(cwd) / LOG_DIR / LOG_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    rec: Dict[str, Any] = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    for k, v in fields.items():
        rec[k] = clean(v, 200) if isinstance(v, str) else v
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _log_decision(decision: dict) -> None:
    _append_log(decision["cwd"], {
        "session_id": decision["session_id"], "tool": decision["tool"], "mode": decision["mode"],
        "decision": decision["decision"], "policy_decision": decision["policy_decision"],
        "codes": decision["codes"], "approval": decision["approval"],
        "summary": decision["summary"], "reason": decision["reason"],
    })


def _log_warnings(decision: dict) -> None:
    for text in decision.get("warnings") or []:
        _append_log(decision["cwd"], {"session_id": decision["session_id"],
                                      "decision": "warning", "reason": text})


def _log_quietly(fn: Callable[[], None]) -> None:
    try:
        fn()
    except Exception:  # a log that cannot be written must not change the decision
        pass


# ---------------------------------------------------------------- hook entry point

def handle(raw: str) -> Optional[dict]:
    """Hook input (JSON text) to hook output dict, or None to print nothing.
    Never raises."""
    event: Any = None
    policy: Dict[str, Any] = {}
    try:
        event = json.loads(raw)
        if not isinstance(event, dict):
            raise ValueError("hook input is not a JSON object")
        policy = read_policy(_event_cwd(event))
        decision = decide(event, policy)
    except Exception as exc:
        return _on_error(event, policy, exc)
    _log_quietly(lambda: _log_warnings(decision))
    _log_quietly(lambda: _log_decision(decision))
    return hook_output(decision)


def _on_error(event: Any, policy: Dict[str, Any], exc: BaseException) -> Optional[dict]:
    try:
        cwd = _event_cwd(event)
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
        _log_quietly(lambda: _append_log(cwd, {
            "session_id": session, "tool": tool, "mode": cfg["mode"], "decision": "error",
            "codes": [], "reason": msg}))
    if cfg["mode"] == "monitor":
        return None
    if cfg["fail_closed"]:
        return _deny_output(f"RunLedger guard could not evaluate this call (fail_closed is on): {msg}")
    return None


def main() -> int:
    """Entry point for `runledger guard` as a hook: stdin JSON in, stdout JSON out.
    Always exits 0."""
    try:
        raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
    except Exception:
        raw = ""
    try:
        out = handle(raw)
    except Exception:
        out = None
    if out is not None:
        try:
            sys.stdout.write(json.dumps(out) + "\n")
            sys.stdout.flush()
        except Exception:
            pass
    return 0


# ---------------------------------------------------------------- CLI helpers

def cmd_test(event_json: str) -> int:
    """`runledger guard test '<json event>'`: print the decision. Writes no log."""
    try:
        event = json.loads(event_json)
    except ValueError as exc:
        print(f"error: event is not valid JSON: {exc}", file=sys.stderr)
        return 1
    if not isinstance(event, dict):
        print("error: event must be a JSON object", file=sys.stderr)
        return 1
    try:
        decision = decide(event, read_policy(_event_cwd(event)))
    except Exception as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"decision": decision, "hook_output": hook_output(decision)}, indent=2))
    return 0


def settings_path(project: Optional[str] = None, global_scope: bool = False,
                  home: Optional[Path] = None) -> Path:
    if global_scope:
        return (Path(home) if home is not None else Path.home()) / ".claude" / "settings.json"
    return Path(project or os.getcwd()) / ".claude" / "settings.json"


def hook_timeout_for(approval_timeout_s: Optional[float] = None) -> int:
    """The hook's "timeout" in seconds: the approval wait plus HOOK_MARGIN_S, rounded up.
    Claude Code stops a hook at its timeout and then lets the tool run, so the hook must
    outlive the approval wait. None means the default approval wait (120 s, so 135)."""
    base = TIMEOUT_DEFAULT_S if approval_timeout_s is None else float(approval_timeout_s)
    return int(math.ceil(base)) + HOOK_MARGIN_S


def hook_entry(hook_seconds: int) -> Dict[str, Any]:
    return {"matcher": "*", "hooks": [{"type": "command", "command": HOOK_COMMAND, "timeout": hook_seconds}]}


def _is_guard_hook(h: Any) -> bool:
    return isinstance(h, dict) and isinstance(h.get("command"), str) and bool(_GUARD_CMD_RE.search(h["command"]))


def _group_has_guard(group: Any) -> bool:
    if not isinstance(group, dict):
        return False
    hooks = group.get("hooks")
    return any(_is_guard_hook(h) for h in (hooks if isinstance(hooks, list) else []))


def install_hook(path: Path, approval_timeout_s: Optional[float] = None) -> Tuple[bool, Optional[Path]]:
    """Add the PreToolUse guard entry to a settings.json, keeping everything else.

    The entry's "timeout" is hook_timeout_for(approval_timeout_s). An existing guard
    hook (any matcher) gets its timeout updated in place; a second entry is never added.

    Returns (changed, backup). The original is copied to `settings.json.bak` before
    any change; a file that is already up to date is not written. Raises InstallError
    (and writes nothing) if the file is not a JSON object."""
    path = Path(path)
    want = hook_timeout_for(approval_timeout_s)
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
            if _is_guard_hook(h) and h.get("timeout") != want:
                h["timeout"] = want
                changed = True
    if not found:
        pre.append(hook_entry(want))
        changed = True
    if not changed:
        return False, None

    hooks["PreToolUse"] = pre
    settings["hooks"] = hooks
    return True, _write_settings(path, settings, existed)


def uninstall_hook(path: Path) -> Tuple[bool, Optional[Path]]:
    """Remove every RunLedger guard hook from a settings.json, keeping everything else.

    Other hooks in the same PreToolUse group stay. A group left with no hooks is removed,
    then an empty PreToolUse list and an empty hooks object. Returns (changed, backup);
    the original is copied to `settings.json.bak` first, and a file without the hook is
    not written. Raises InstallError (and writes nothing) if the file is not a JSON object."""
    path = Path(path)
    existed, settings = _read_settings(path)
    hooks = settings.get("hooks")
    pre = hooks.get("PreToolUse") if isinstance(hooks, dict) else None
    if not isinstance(pre, list):
        return False, None
    kept: List[Any] = []
    removed = False
    for group in pre:
        if not _group_has_guard(group):
            kept.append(group)
            continue
        removed = True
        rest = [h for h in group["hooks"] if not _is_guard_hook(h)]
        if rest:
            kept.append(dict(group, hooks=rest))
    if not removed:
        return False, None
    if kept:
        hooks["PreToolUse"] = kept
    else:
        del hooks["PreToolUse"]
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


def cmd_install(project: Optional[str] = None, global_scope: bool = False,
                home: Optional[Path] = None) -> int:
    """Install or update the hook. The hook timeout follows the approval wait: the project's
    timeout_s for a project install, the user's or the default for a global install."""
    target = settings_path(project, global_scope, home)
    project_policy = {} if global_scope else read_policy(str(Path(project or os.getcwd())))
    timeout_s = approval_timeout(project_policy)
    try:
        changed, backup = install_hook(target, timeout_s)
    except InstallError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: cannot update {target}: {exc}", file=sys.stderr)
        return 1
    hook_s = hook_timeout_for(timeout_s)
    if changed:
        print(f"Installed or updated the RunLedger guard hook in {target} (hook timeout {hook_s} s)")
        if backup is not None:
            print(f"Backup of the previous file: {backup}")
    else:
        print(f"The RunLedger guard hook in {target} is already up to date (hook timeout {hook_s} s). Nothing changed.")
    return 0


def cmd_uninstall(project: Optional[str] = None, global_scope: bool = False,
                  home: Optional[Path] = None) -> int:
    """Remove the hook from the project's or the user's Claude Code settings."""
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
        print(f"No RunLedger guard hook in {target}. Nothing changed.")
        return 0
    print(f"Removed the RunLedger guard hook from {target}")
    if backup is not None:
        print(f"Backup of the previous file: {backup}")
    print("Policy files (.runledger.json, ~/.runledger/config.json) and .runledger/guard.log were left as they are.")
    return 0
