"""`runledger doctor` and `runledger guard status`: the nine checks of the v0.7.0 design.

The report is read-only: it never writes a hook file, a config file, a `.runledger` folder or a
Git ref, and the only network request it may make is an unauthenticated `GET <server>/health`
for the approval check (skipped with `--offline`). No secret is ever printed: the approval key
is reported as `set (env RUNLEDGER_API_KEY)` or `not set`.

Checks (design section 3.2): `install`, `guard.hooks` (per agent and scope), `guard.note`
(Codex trust, Droid restart), `guard.selftest`, `config`, `approval`, `log`, `rollback`,
`pricing`, `sessions`. `guard status` runs the guard subset only: hooks, notes, config,
approval, log and the effective mode, without the self-test.

Exit codes: 0 when every check is ok/info/skip, 1 with at least one warning, 2 with at least
one failure (the highest severity decides).
"""
from __future__ import annotations

import contextlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import hook_targets
from . import paths as _paths
from . import pricing
from . import rollback as _rollback
from . import guard as _guard
from .hook_targets import AGENT_CLAUDE, AGENT_CODEX, AGENT_DROID, AGENTS
from .userconfig import user_config_path

__all__ = ["Check", "Report", "run_checks", "run_guard_status", "summary", "exit_code",
           "render_text", "render_json", "cmd_doctor", "cmd_guard_status"]

STATUSES = ("ok", "warn", "fail", "info", "skip")
OK, WARN, FAIL, INFO, SKIP = STATUSES
JSON_VERSION = 1
SCOPES = ("project", "global")
SESSION_AGENTS = ("claude-code", "codex", "droid", "aider", "native")
CHECKPOINT_EVENTS = ("SessionStart", "UserPromptSubmit")
CHECKPOINT_WARN_COUNT = 100
LOG_ERROR_DAYS = 7
HEALTH_TIMEOUT_S = 3.0
SELFTEST_TIMEOUT_S = 10.0
DETAIL_HEAD = 200
# Codex runs the `commandWindows` copy of a stored command on Windows and `command` everywhere
# else, so the platform decides which one doctor inspects and self-tests.
_IS_WINDOWS = sys.platform == "win32"

PROJECT_POLICY_FILE = ".runledger.json"
# The top-level keys each file may carry; anything else is reported as unknown (info).
KNOWN_USER_KEYS = ("guard", "ignore", "severity_overrides", "extra_secret_paths", "mcp_verbs",
                   "tool_rules", "command_rules", "rollback", "server", "api_key_env", "push_safe")
KNOWN_PROJECT_KEYS = ("guard", "ignore", "severity_overrides", "extra_secret_paths", "mcp_verbs",
                      "tool_rules", "command_rules")
REINSTALL_FIX = ("Re-run `runledger guard install` for this agent and scope to rewrite the "
                 "entry, or remove the file if the hooks are not wanted.")
SELFTEST_FIX = ("Re-run `runledger guard install` for this agent: the stored command must run "
                "and deny a destructive call.")


def _project_dir(project: Optional[str]) -> str:
    """The project folder to report on: `--project`, else the resolved project root."""
    if project:
        return str(Path(project).expanduser())
    return _guard.project_root()


def _selected_agents(agents: Optional[Sequence[str]]) -> List[str]:
    if not agents:
        return list(AGENTS)
    return [a for a in AGENTS if a in set(agents)]


@dataclass
class Check:
    """One doctor finding. `agent` and `scope` are None for agent-independent checks."""

    id: str
    status: str
    title: str
    detail: str
    fix: str = ""
    agent: Optional[str] = None
    scope: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "status": self.status, "title": self.title,
                "detail": self.detail, "fix": self.fix, "agent": self.agent, "scope": self.scope}


@dataclass
class Report:
    """A doctor run: the project it reports on and every check it made."""

    project: str
    checks: List[Check] = field(default_factory=list)

    @property
    def status(self) -> str:
        return _worst(self.checks)

    def __iter__(self):
        return iter(self.checks)

    def __len__(self) -> int:
        return len(self.checks)


def _worst(checks: Sequence[Check]) -> str:
    if any(c.status == FAIL for c in checks):
        return FAIL
    if any(c.status == WARN for c in checks):
        return WARN
    if checks and all(c.status == SKIP for c in checks):
        return SKIP
    return OK


def summary(checks: Sequence[Check]) -> Dict[str, int]:
    """How many checks have each status (`skip` is not part of the summary line)."""
    return {status: sum(1 for c in checks if c.status == status) for status in ("ok", "warn", "fail", "info")}


def exit_code(checks: Sequence[Check]) -> int:
    """0 ok/info/skip, 1 with a warning, 2 with a failure (the highest severity decides)."""
    status = _worst(checks)
    return 2 if status == FAIL else (1 if status == WARN else 0)


# ---------------------------------------------------------------- checks

def _install_check(project: str) -> Check:
    from . import __version__

    version = platform.python_version()
    how = shutil.which("runledger")
    if how:
        reach = f"runledger on PATH ({how})"
        status = OK
    else:
        reach = "not on PATH, running as `python -m runledger`"
        status = INFO
    detail = (f"runledger {__version__} | python {version} | {sys.platform} | {project} | {reach}")
    if sys.version_info < (3, 9):
        return Check("install", FAIL, "install", detail,
                     "RunLedger needs Python 3.9 or newer.", None, None)
    return Check("install", status, "install", detail)


def _hook_check(root: str, agent: str, scope: str, user: Dict[str, Any],
                project_policy: Dict[str, Any]) -> List[Check]:
    """The `guard.hooks` entry (or entries) for one agent and scope."""
    target = hook_targets.target_for(agent, root, global_scope=(scope == "global"))
    path = Path(target.path)
    who = f"{hook_targets.LABEL[agent]} {scope}"

    def check(status: str, detail: str, fix: str = "") -> Check:
        return Check("guard.hooks", status, "guard hooks", f"{who}: {detail}", fix=fix,
                     agent=agent, scope=scope)

    if not path.exists():
        return [check(INFO, f"not installed ({path})")]
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        return [check(FAIL, f"cannot read {path}: {exc}",
                      f"Fix the file permissions of {path} or remove it, then re-run "
                      f"`runledger guard install`.")]
    if not text.strip():
        return [check(INFO, f"not installed ({path} is empty)")]
    try:
        data = json.loads(text)
    except ValueError as exc:
        where = f" (line {getattr(exc, 'lineno', '?')}, column {getattr(exc, 'colno', '?')})"
        return [check(FAIL, f"{path} is not valid JSON: {getattr(exc, 'msg', exc)}{where}",
                      f"Fix or move {path}, then re-run `runledger guard install`.")]
    if not isinstance(data, dict):
        return [check(FAIL, f"{path} must contain a JSON object",
                      f"Fix or move {path}, then re-run `runledger guard install`.")]

    if target.wrap_key is None:
        events: Any = data
    else:
        events = data.get(target.wrap_key)
        if events is None:
            return [check(INFO, f"not installed (no `{target.wrap_key}` in {path})")]
    if not isinstance(events, dict):
        return [check(FAIL, f"`{target.wrap_key}` in {path} must be an object",
                      f"Fix or move {path}, then re-run `runledger guard install`.")]

    guard_hooks = [h for group in _groups(events, "PreToolUse")
                   for h in _entries(group) if _guard._is_guard_hook(h)]
    checkpoint_hooks = {event: [h for group in _groups(events, event)
                                for h in _entries(group) if _rollback.is_checkpoint_hook(h)]
                        for event in CHECKPOINT_EVENTS}
    if not guard_hooks and not any(checkpoint_hooks.values()):
        return [check(INFO, f"not installed (no RunLedger hooks in {path})")]

    issues: List[Tuple[str, str, str]] = []
    if not guard_hooks:
        issues.append((WARN, f"the guard hook is missing from {path}", REINSTALL_FIX))
    if len(guard_hooks) > 1:
        issues.append((WARN, f"{len(guard_hooks)} guard entries in {path}: the guard would run "
                             f"once per entry", REINSTALL_FIX))
    missing = [event for event in CHECKPOINT_EVENTS if not checkpoint_hooks[event]]
    if missing:
        issues.append((WARN, f"the {' and '.join(missing)} checkpoint hook is missing from {path}",
                       REINSTALL_FIX))
    for entry in guard_hooks:
        issues.extend(_command_issues(entry, agent, scope, path, user, project_policy))
    if not issues:
        found = ("guard hook (PreToolUse)" if guard_hooks else "no guard hook")
        detail = (f"{found} and {len(checkpoint_hooks['SessionStart'])} + "
                  f"{len(checkpoint_hooks['UserPromptSubmit'])} checkpoint hooks "
                  f"(SessionStart, UserPromptSubmit) in {path}")
        return [check(OK, detail)]
    status = _worst([Check("x", s, "t", "d") for s, _, _ in issues])
    detail = f"{who}: " + "; ".join(text for _, text, _ in issues)
    fix = _first_fix(issues) or REINSTALL_FIX
    return [check(status, detail[len(who) + 2:], fix)]


def _first_fix(issues: Sequence[Tuple[str, str, str]]) -> str:
    for status, _, fix in issues:
        if status in (FAIL, WARN) and fix:
            return fix
    return ""


def _command_issues(entry: Dict[str, Any], agent: str, scope: str, path: Path,
                    user: Dict[str, Any], project_policy: Dict[str, Any]
                    ) -> List[Tuple[str, str, str]]:
    """What is wrong with one stored guard hook entry, as (status, text, fix)."""
    issues: List[Tuple[str, str, str]] = []
    command = entry.get("command")
    parts = _guard._owned_command_parts(command)
    if not parts:
        return issues
    scope_flag = "--global" if scope == "global" else f"--project {path.parent.parent}"
    reinstall = (f"Re-run `runledger guard install --agent {agent} {scope_flag}` "
                 f"to rewrite the entry.")
    issues.extend(_interpreter_issues(parts, command, agent, scope_flag, reinstall))
    windows = _windows_override(agent, entry)
    if windows is not None:
        windows_parts = _windows_command_parts(windows)
        if windows_parts is None:
            issues.append((FAIL, f"the `commandWindows` value {windows!r} is not a RunLedger "
                                 f"guard command: on Windows Codex would run that instead",
                           reinstall))
        else:
            issues.extend(_interpreter_issues(windows_parts, windows, agent, scope_flag, reinstall))
    # The hook runs inside the project, so its effective approval wait is the project's,
    # for a global entry too (guard.py reads the same policy at run time).
    wait = _guard.approval_timeout(project_policy, user)
    required = _guard.hook_timeout_for(wait)
    timeout = entry.get("timeout")
    if not _is_number(timeout) or float(timeout) < required:
        issues.append((WARN, f"the hook timeout is {_show(timeout)} but the approval wait needs "
                             f"{required}s: the agent would run the tool before an approval "
                             f"answers", reinstall))
    return issues


def _interpreter_issues(parts: Sequence[str], command: str, agent: str, scope_flag: str,
                        reinstall: str) -> List[Tuple[str, str, str]]:
    """What is wrong with the interpreter one stored guard command starts with."""
    if _paths.is_runledger_executable(parts[0]):
        if not shutil.which(parts[0]):
            return [(WARN, f"the hook command {command!r} depends on PATH and "
                           f"`{parts[0]}` was not found on it",
                     f"Re-run `runledger guard install --agent {agent} {scope_flag}` "
                     f"without `--portable`, or add `{parts[0]}` to PATH.")]
        return []
    interpreter = parts[0]
    if not (Path(interpreter).is_absolute() and Path(interpreter).exists()) \
            and not shutil.which(interpreter):
        return [(FAIL, f"the hook command interpreter {interpreter!r} does not exist", reinstall)]
    return []


_CMD_C = re.compile(r"^\s*cmd(?:\.exe)?\s+/c(?:\s+|$)", re.IGNORECASE)


def _windows_override(agent: str, entry: Dict[str, Any]) -> Optional[str]:
    """The `commandWindows` string Codex runs on Windows instead of `command`, when stored."""
    if agent != AGENT_CODEX or not _IS_WINDOWS:
        return None
    value = entry.get("commandWindows")
    return value if isinstance(value, str) and value.strip() else None


def _windows_command_parts(value: str) -> Optional[List[str]]:
    """The RunLedger words of a stored `commandWindows` value, else None.

    The installer writes `cmd /c "<command>"`; the same command wrapped without the inner
    quotes (`cmd /c <command>`) is what Codex runs there too, so it counts as ours as well."""
    text = value.strip()
    prefix = _CMD_C.match(text)
    if prefix:
        text = text[prefix.end():].strip()
        if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
            text = text[1:-1]
    return _guard._owned_command_parts(text)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _show(value: Any) -> str:
    return "missing" if value is None else f"{value:g}s" if _is_number(value) else repr(value)


def _groups(events: Dict[str, Any], event: str) -> List[Any]:
    groups = events.get(event)
    return groups if isinstance(groups, list) else []


def _entries(group: Any) -> List[Any]:
    if not isinstance(group, dict):
        return []
    hooks = group.get("hooks")
    return hooks if isinstance(hooks, list) else []


def _hook_files(root: str, agent: str) -> List[Path]:
    return [Path(hook_targets.target_for(agent, root, global_scope=(scope == "global")).path)
            for scope in SCOPES]


def _guard_entries_in(path: Path, agent: str) -> List[Dict[str, Any]]:
    """The stored RunLedger guard hook entries of one file; unreadable files give []."""
    return [entry for entry, _ in _pre_tool_use_entries(path, agent)
            if _guard._is_guard_hook(entry)]


def _note_checks(root: str, agents: Sequence[str]) -> List[Check]:
    """The Codex trust and Droid restart notes, for every agent whose hook is installed."""
    out: List[Check] = []
    for agent in agents:
        if not any(_guard_entries_in(path, agent) for path in _hook_files(root, agent)):
            continue
        if agent == AGENT_CODEX:
            detail = ("Codex runs only hooks you trust: open `codex` and review the RunLedger "
                      "entries in /hooks. RunLedger cannot read that trust state, so it cannot "
                      "confirm the hook will run.")
        elif agent == AGENT_DROID:
            detail = ("Droid snapshots its hooks at startup: restart droid, or review the "
                      "RunLedger entries in /hooks, before the guard takes effect.")
        else:
            continue
        out.append(Check("guard.note", INFO, "guard notes", f"{hook_targets.LABEL[agent]}: {detail}",
                         agent=agent))
    return out


# ---------------------------------------------------------------- the run

def run_checks(project: Optional[str] = None, agents: Optional[Sequence[str]] = None,
               offline: bool = False, selftest: bool = True,
               guard_only: bool = False, today: Optional[date] = None) -> Report:
    """Every check for one project. Read-only; the self-test runs in a temp folder."""
    root = _project_dir(project)
    selected = _selected_agents(agents)
    user = _guard.load_user_config()
    project_policy = _guard.read_policy(root, _resolved=True)

    checks: List[Check] = []
    if not guard_only:
        checks.append(_install_check(root))
    for agent in selected:
        for scope in SCOPES:
            checks.extend(_hook_check(root, agent, scope, user, project_policy))
    checks.extend(_note_checks(root, selected))
    if guard_only:
        pass  # `guard status` never runs the hook self-test
    elif selftest:
        checks.extend(_selftest_checks(root, selected, user))
    else:
        checks.extend(_skip_selftest())
    checks.extend(_config_checks(root, user, project_policy))
    checks.append(_approval_check(user, offline))
    checks.append(_log_check(root))
    if guard_only:
        checks.append(_mode_check(user, project_policy))
    else:
        checks.append(_rollback_check(root))
        checks.append(_pricing_check(today))
        checks.extend(_sessions_checks(root, agents))
    return Report(project=root, checks=checks)


def run_guard_status(project: Optional[str] = None, agents: Optional[Sequence[str]] = None,
                     today: Optional[date] = None) -> Report:
    """The compact guard-only view: no install, pricing, rollback or sessions checks."""
    return run_checks(project=project, agents=agents, offline=False, selftest=False,
                      guard_only=True, today=today)


def _skip_selftest() -> List[Check]:
    return [Check("guard.selftest", SKIP, "hook self-test",
                  "not run (--no-selftest); no hook subprocess was started")]


# ---------------------------------------------------------------- the self-test

def _selftest_checks(root: str, agents: Sequence[str], user: Dict[str, Any]) -> List[Check]:
    """Run the stored hook command of every installed agent once, in a temp folder."""
    out: List[Check] = []
    folder: Optional[Path] = None
    env: Dict[str, str] = {}
    try:
        for agent in agents:
            found = _stored_command(root, agent)
            if found is None:
                continue
            command, scope = found
            if folder is None:
                folder = Path(tempfile.mkdtemp(prefix="runledger-doctor-"))
                env = _selftest_env(folder, user)
            out.append(_selftest_check(agent, scope, command, env, str(folder)))
    finally:
        if folder is not None:
            shutil.rmtree(str(folder), ignore_errors=True)
    return out


def _stored_command(root: str, agent: str) -> Optional[Tuple[str, str]]:
    """`(command, scope)` of the PreToolUse hook the agent would run for a tool call.

    RunLedger's own guard entry comes first, and for Codex the `commandWindows` copy is the
    command that agent runs on Windows. A file whose guard entry was hand-edited no longer
    matches the installed shape, so any entry the agent would run for every tool is used
    instead - but only in a file that still stores RunLedger's own hooks: a foreign PreToolUse
    hook is never run as the guard's self-test, and it never hides a RunLedger entry that is
    installed in another scope."""
    for scope in SCOPES:
        path = Path(hook_targets.target_for(agent, root, global_scope=(scope == "global")).path)
        entries = _pre_tool_use_entries(path, agent)
        if not entries:
            continue
        chosen = [e for e, _ in entries if _guard._is_guard_hook(e)]
        if not chosen and _holds_runledger_hooks(path, agent):
            chosen = [e for e, matcher in entries if matcher in ("", "*", ".*")]
            if not chosen:
                chosen = [e for e, _ in entries]
        for entry in chosen:
            command = entry.get("command")
            if isinstance(command, str) and command.strip():
                return _windows_override(agent, entry) or command, scope
    return None


def _holds_runledger_hooks(path: Path, agent: str) -> bool:
    """True when that agent's file still stores a RunLedger hook of its own (a guard or a
    checkpoint entry): the evidence the edited-hook fallback needs before running an entry
    that no longer matches the installed shape."""
    for event in ("PreToolUse",) + CHECKPOINT_EVENTS:
        for entry, _ in _stored_entries(path, agent, event):
            if _guard._is_guard_hook(entry) or _rollback.is_checkpoint_hook(entry):
                return True
    return False


def _pre_tool_use_entries(path: Path, agent: str) -> List[Tuple[Dict[str, Any], str]]:
    """`(entry, matcher)` for every command entry of that agent's PreToolUse section."""
    return _stored_entries(path, agent, "PreToolUse")


def _stored_entries(path: Path, agent: str, event: str) -> List[Tuple[Dict[str, Any], str]]:
    """`(entry, matcher)` for every command entry of one event section; unreadable gives []."""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    wrap = hook_targets.WRAP_KEY[agent]
    events = data if wrap is None else data.get(wrap)
    if not isinstance(events, dict):
        return []
    out: List[Tuple[Dict[str, Any], str]] = []
    for group in _groups(events, event):
        matcher = group.get("matcher") if isinstance(group, dict) else None
        for entry in _entries(group):
            if isinstance(entry, dict) and isinstance(entry.get("command"), str):
                out.append((entry, matcher if isinstance(matcher, str) else ""))
    return out


def _selftest_env(folder: Path, user: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    """The environment the hook runs in: a temp HOME, no approval server, no key, no project
    directory variable, so the run cannot reach the real config or pollute the project."""
    env = dict(os.environ)
    key_env = _guard.trusted_approval_settings(user or {}).get("api_key_env") or ""
    for name in ("RUNLEDGER_SERVER", "RUNLEDGER_API_KEY", "RUNLEDGER_CONFIG", "RUNLEDGER_PUSH_SAFE",
                 key_env, *_guard.PROJECT_DIR_ENV_VARS):
        if name:
            env.pop(name, None)
    home = folder / "home"
    home.mkdir(parents=True, exist_ok=True)
    env.update({"HOME": str(home), "USERPROFILE": str(home),
                "CLAUDE_CONFIG_DIR": str(home / ".claude"),
                "CODEX_HOME": str(home / ".codex"),
                "FACTORY_HOME_OVERRIDE": str(home)})
    env.pop("FACTORY_HOME", None)
    return env


def _selftest_payload(agent: str, cwd: str, command: str) -> Dict[str, Any]:
    """A synthetic PreToolUse event in that agent's recorded shape."""
    base: Dict[str, Any] = {"session_id": "runledger-doctor-selftest",
                            "transcript_path": str(Path(cwd) / "selftest.jsonl"),
                            "cwd": cwd, "hook_event_name": "PreToolUse"}
    if agent == AGENT_DROID:
        return dict(base, permission_mode="auto-low", tool_name="Execute",
                    tool_input={"command": command, "summary": "RunLedger doctor self-test",
                                "riskLevel": "high"})
    if agent == AGENT_CODEX:
        return dict(base, turn_id="runledger-doctor-selftest", model="gpt-6-luna",
                    permission_mode="bypassPermissions", tool_name="Bash",
                    tool_input={"command": command})
    return dict(base, prompt_id="runledger-doctor-selftest", permission_mode="bypassPermissions",
                tool_name="Bash",
                tool_input={"command": command, "description": "RunLedger doctor self-test"})


def _run_hook_command(command: str, payload: Dict[str, Any], env: Dict[str, str],
                      cwd: str) -> Tuple[int, str, str, float]:
    """`(exit code, stdout, stderr, seconds)`. -1 means the command never started.

    The stored command goes through the shell, which is what the agent does: on Windows
    `cmd /c "<command>"` (Codex's own `commandWindows` form is the same string cmd builds from
    the plain command)."""
    started = time.time()
    try:
        proc = subprocess.run(command, shell=True, input=json.dumps(payload).encode("utf-8"),
                              capture_output=True, timeout=SELFTEST_TIMEOUT_S, cwd=cwd, env=env)
    except subprocess.TimeoutExpired:
        return -1, "", f"timed out after {SELFTEST_TIMEOUT_S:g}s", time.time() - started
    except OSError as exc:
        return -1, "", f"{type(exc).__name__}: {exc}", time.time() - started
    return (proc.returncode, _decode(proc.stdout), _decode(proc.stderr), time.time() - started)


def _decode(raw: bytes) -> str:
    return (raw or b"").decode("utf-8", errors="replace")


def _selftest_check(agent: str, scope: str, command: str, env: Dict[str, str], cwd: str) -> Check:
    problems: List[str] = []
    spent = 0.0
    for command_text, want in (("rm -rf /", "deny"), ("ls", "silent")):
        code, out, err, seconds = _run_hook_command(
            command, _selftest_payload(agent, cwd, command_text), env, cwd)
        spent += seconds
        observed = _observed(code, out, err)
        if code != 0:
            problems.append(f"`{command_text}` -> {observed}")
            continue
        decision = _permission_decision(out)
        if want == "deny" and decision != "deny":
            problems.append(f"`{command_text}` was not denied -> {observed}")
        elif want == "silent" and out.strip():
            problems.append(f"the hook printed output for `{command_text}` -> {observed}")
    if problems:
        return Check("guard.selftest", FAIL, "hook self-test",
                     f"{hook_targets.LABEL[agent]} {scope}: {command} {problems[0]}",
                     fix=SELFTEST_FIX, agent=agent)
    return Check("guard.selftest", OK, "hook self-test",
                 f"{hook_targets.LABEL[agent]} {scope}: {command} denies `rm -rf /` (JSON deny) "
                 f"and stays silent for `ls` in {_millis(spent)}", agent=agent)


def _millis(seconds: float) -> str:
    """A duration a user can compare against the hook timeout."""
    if seconds >= 1:
        return f"{seconds:.1f} s"
    return f"{int(round(seconds * 1000))} ms"


def _observed(code: int, out: str, err: str) -> str:
    if code == -1:
        return f"did not start ({_head(err)})"
    return (f"exit code {code}; stdout: {_head(out) or 'empty'}; "
            f"stderr: {_head(err) or 'empty'}")


def _head(text: str) -> str:
    return " ".join((text or "").split())[:DETAIL_HEAD]


def _permission_decision(out: str) -> Optional[str]:
    try:
        data = json.loads(out.strip().splitlines()[0] if out.strip() else "")
    except (ValueError, IndexError):
        return None
    if not isinstance(data, dict):
        return None
    inner = data.get("hookSpecificOutput")
    if isinstance(inner, dict) and isinstance(inner.get("permissionDecision"), str):
        return inner["permissionDecision"]
    if isinstance(data.get("permissionDecision"), str):
        return data["permissionDecision"]
    return None


# ---------------------------------------------------------------- config

def _read_config(path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """`(data, error)`. A missing file is `(None, None)`; a broken one carries the reason."""
    if not path.exists():
        return None, None
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return None, f"cannot read {path}: {exc}"
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        return None, (f"{path} is not UTF-8 text ({_one_line(exc)}); the guard and the receipts "
                      f"read it as UTF-8, so re-save it (a UTF-16 file comes from a shell "
                      f"redirection)")
    if not text.strip():
        return None, f"{path} is empty: it holds no JSON object"
    try:
        data = json.loads(text)
    except ValueError as exc:
        where = f" (line {getattr(exc, 'lineno', '?')}, column {getattr(exc, 'colno', '?')})"
        return None, f"{path} is not valid JSON: {getattr(exc, 'msg', exc)}{where}"
    if not isinstance(data, dict):
        return None, f"{path} must contain a JSON object"
    return data, None


def _config_checks(root: str, user: Dict[str, Any], project_policy: Dict[str, Any]) -> List[Check]:
    return [_config_entry("user", user_config_path(), KNOWN_USER_KEYS, user, root),
            _config_entry("project", Path(root) / PROJECT_POLICY_FILE, KNOWN_PROJECT_KEYS,
                          project_policy, root, user)]


def _config_entry(kind: str, path: Path, known: Sequence[str], data: Dict[str, Any],
                  root: str, user: Optional[Dict[str, Any]] = None) -> Check:
    parsed, error = _read_config(path)
    if error:
        return Check("config", FAIL, "config", error,
                     f"Fix or remove {path}: the guard and the receipts read it as UTF-8 JSON "
                     f"and ignore it while it is not.")
    if parsed is None:
        return Check("config", INFO, "config", f"no {kind} config file ({path})")
    # A project file may only tighten the guard, so what it tries to loosen is reported;
    # the user's file is the baseline that decision is measured against.
    ignored = _ignored_project_keys(parsed, user or {}) if kind == "project" else []
    invalid = _invalid_rule_count(parsed)
    unknown = sorted(k for k in parsed if k not in known)

    parts: List[str] = []
    if unknown:
        parts.append(f"unknown top-level key(s): {', '.join(unknown)}")
    if kind == "project":
        parts.append(f"{len(ignored)} ignored key(s)")
        parts.extend(ignored)
    if invalid:
        parts.append(f"{invalid} invalid rule entry(ies) were dropped")
    detail = f"{path}: valid" + ("; " + "; ".join(parts) if parts else "")
    if ignored or invalid:
        fix = ("Fix or remove the invalid rule entries (deny/ask, command_rules, tool_rules, "
               "mcp_verbs); see docs/guard.md.")
        if kind == "project" and not invalid:
            fix = ("Remove those keys from .runledger.json: a project file may only tighten the "
                   "guard, not loosen it.")
        return Check("config", WARN, "config", detail, fix=fix)
    if unknown:
        return Check("config", INFO, "config", detail)
    return Check("config", OK, "config", detail)


def _ignored_project_keys(project: Dict[str, Any], user: Dict[str, Any]) -> List[str]:
    """The project settings the guard ignores, each as the sentence the guard would log."""
    texts: List[str] = []
    _, warnings = _guard._effective_guard(project, user, {"server": "", "api_key_env": ""})
    texts.extend(warnings)
    texts.extend(_guard._project_key_warnings(project))
    user_overrides = _guard._valid_overrides(user.get("severity_overrides"))
    for code, level in _guard._valid_overrides(project.get("severity_overrides")).items():
        base = user_overrides.get(code)
        # `low` can never raise a risk, and a level at or below the user's own cannot either.
        if level == "low" or (base is not None
                              and _risk_severity_points(level) <= _risk_severity_points(base)):
            texts.append(f"Ignored severity_overrides for {code} in {PROJECT_POLICY_FILE}: "
                         f"a project may raise a risk's severity, not lower it.")
    return texts


def _risk_severity_points(level: str) -> int:
    from .risk import SEVERITY_POINTS

    return SEVERITY_POINTS.get(level, 0)


def _invalid_rule_count(data: Dict[str, Any]) -> int:
    """How many rule entries in one config file are typed wrongly or dropped as unsafe."""
    from . import risk as _risk

    count = 0
    guard_section = _guard._section(data, "guard")
    for kind in ("deny", "ask"):
        value = guard_section.get(kind)
        if value is None:
            continue
        if not isinstance(value, list):
            count += 1
            continue
        count += sum(1 for entry in value if _guard._parse_rule(entry) is None)
    count += _bad_command_rules(data.get("command_rules"))
    count += _bad_tool_rules(data.get("tool_rules"))
    count += _bad_mcp_verbs(data.get("mcp_verbs"))
    count += len(_guard._valid_extra_with_warnings(data.get("extra_secret_paths"))[1])
    return count


def _bad_command_rules(value: Any) -> int:
    from . import risk as _risk

    if value is None:
        return 0
    if not isinstance(value, dict):
        return 1
    count = 0
    for kind in ("deny", "ask"):
        entries = value.get(kind)
        if entries is None:
            continue
        if not isinstance(entries, list):
            count += 1
            continue
        for entry in entries:
            pattern = entry.get("pattern") if isinstance(entry, dict) else entry
            if not isinstance(pattern, str) or not pattern.strip():
                count += 1
    _, warnings = _risk.parse_command_rules(value, "the config file")
    return count + len(warnings)


def _bad_tool_rules(value: Any) -> int:
    if value is None:
        return 0
    if not isinstance(value, dict):
        return 1
    count = 0
    for kind in ("deny", "ask"):
        entries = value.get(kind)
        if entries is None:
            continue
        if not isinstance(entries, list):
            count += 1
            continue
        count += sum(1 for entry in entries if not (isinstance(entry, str) and entry.strip()))
    return count


def _bad_mcp_verbs(value: Any) -> int:
    if value is None:
        return 0
    if not isinstance(value, dict):
        return 1
    count = 0
    for kind in ("destructive", "write", "read"):
        words = value.get(kind)
        if words is None:
            continue
        if not isinstance(words, list):
            count += 1
            continue
        count += sum(1 for word in words if not (isinstance(word, str) and word.strip()))
    return count


# ---------------------------------------------------------------- approval server

def _approval_check(user: Dict[str, Any], offline: bool) -> Check:
    """The server and the key name come from the environment or the user's file only,
    and the key's value is never read into the report."""
    trusted = _guard.trusted_approval_settings(user)
    server = trusted["server"]
    key_env = trusted["api_key_env"]
    key_text = (f"key set (env {key_env})" if os.environ.get(key_env, "").strip()
                else f"key {key_env} not set")
    if not server:
        return Check("approval", INFO, "approval server",
                     f"no approval server configured; {key_text} (the guard decides locally)")
    issues: List[str] = []
    scheme, host = _url_parts(server)
    if scheme == "http" and host not in ("127.0.0.1", "localhost", "::1"):
        issues.append(f"the server {server} uses cleartext http outside the loopback interface: "
                      f"use https, or the approval key would travel unencrypted")
    if offline:
        return Check("approval", WARN if issues else INFO, "approval server",
                     f"server {server} "
                     f"({'; '.join(issues) if issues else 'not contacted (--offline)'}); {key_text}",
                     fix=("Use https for the approval server, or keep it on the loopback "
                          "interface." if issues else ""))
    if not issues:
        ok, detail = _health_get(server)
        if ok:
            return Check("approval", OK, "approval server",
                         f"server {server} reachable (/health {detail}); {key_text}")
        issues.append(f"the server {server} is unreachable ({detail})")
    return Check("approval", WARN, "approval server",
                 f"{'; '.join(issues)}; the guard then follows on_timeout; {key_text}",
                 fix=(f"Start the approval server or check {_guard.SERVER_ENV}: while it is "
                      f"unreachable the guard falls back to on_timeout."))


def _url_parts(server: str) -> Tuple[str, str]:
    try:
        parts = urllib.parse.urlsplit(server)
    except ValueError:
        return "", ""
    return (parts.scheme or "").lower(), (parts.hostname or "").lower()


def _health_get(server: str) -> Tuple[bool, str]:
    """An unauthenticated `GET <server>/health` with a 3 s timeout. No key is ever sent.

    Building the request is part of the handled path: a server setting that urllib cannot
    turn into a URL (`approver.example`, no scheme) is an unreachable server, not a crash."""
    url = server.rstrip("/") + "/health"
    try:
        request = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(request, timeout=HEALTH_TIMEOUT_S) as response:
            code = int(getattr(response, "status", 0) or 0)
            return (200 <= code < 300), str(code)
    except urllib.error.HTTPError as exc:
        return False, f"/health returned {exc.code}"
    except Exception as exc:
        return False, _one_line(exc) or type(exc).__name__


def _one_line(exc: BaseException) -> str:
    return " ".join(str(exc).split())[:DETAIL_HEAD]


# ---------------------------------------------------------------- guard log

def _log_check(root: str) -> Check:
    path = Path(root) / _guard.LOG_DIR / _guard.LOG_FILE
    if not path.is_file():
        return Check("log", INFO, "guard log", f"no {path} yet (no hook decisions recorded)")
    try:
        size = path.stat().st_size
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return Check("log", WARN, "guard log", f"cannot read {path}: {_one_line(exc)}",
                     fix=f"Fix the permissions of {path}.")
    horizon = datetime.now(timezone.utc) - timedelta(days=LOG_ERROR_DAYS)
    errors = 0
    rows = 0
    last: Optional[datetime] = None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        rows += 1
        stamp = _parse_stamp(row.get("ts"))
        if stamp is not None and (last is None or stamp > last):
            last = stamp
        if row.get("decision") == "error" and stamp is not None and stamp >= horizon:
            errors += 1
    parts = [f"{size} bytes", f"{rows} row(s)",
             f"last decision {last.isoformat(timespec='seconds')}" if last else "no timestamped rows",
             f"{errors} error row(s) in the last {LOG_ERROR_DAYS} days"]
    status = OK
    fix = ""
    if errors:
        status = WARN
        parts.append("a hook error fails the call open")
        fix = (f"Read the error rows in {_guard.LOG_DIR}/{_guard.LOG_FILE} and remove what "
               f"breaks the hook.")
    tracked = _log_tracked(root)
    ignored = None if tracked else _log_ignored(root)
    if tracked:
        status = WARN
        parts.append("guard.log is tracked by Git: its command summaries are committed and "
                     "would be published even though an ignore rule may cover the path")
        fix = fix or (f"Run `git rm --cached {_guard.LOG_DIR}/{_guard.LOG_FILE}` to untrack the "
                      f"log (keep it on disk), and keep `{_guard.LOG_DIR}/` in .gitignore.")
    elif ignored is False:
        status = WARN
        parts.append(f"{_guard.LOG_DIR}/ is not git-ignored: guard.log holds command summaries")
        fix = fix or (f"Add `{_guard.LOG_DIR}/` to .gitignore: the log holds command summaries "
                      f"you do not want to commit.")
    return Check("log", status, "guard log", f"{path}: " + "; ".join(parts), fix=fix)


def _log_ignored(root: str) -> Optional[bool]:
    """True when `.runledger/guard.log` is git-ignored, False when it is not, None outside a repo."""
    return _git_answers(root, ["check-ignore", "--no-index", "-q", "--",
                               f"{_guard.LOG_DIR}/{_guard.LOG_FILE}"])


def _log_tracked(root: str) -> Optional[bool]:
    """True when `.runledger/guard.log` is in the Git index, False when it is not, None outside.

    An ignore rule does not untrack a file: a log committed before `.gitignore` was added stays
    tracked, keeps recording every later change and can still be published."""
    return _git_answers(root, ["ls-files", "--error-unmatch", "--",
                               f"{_guard.LOG_DIR}/{_guard.LOG_FILE}"])


def _git_answers(root: str, argv: Sequence[str]) -> Optional[bool]:
    """`git -C root <argv>`: True on exit 0, False on exit 1, None when Git cannot tell."""
    if not shutil.which("git"):
        return None
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
    try:
        proc = subprocess.run(["git", "-C", root, *argv], capture_output=True, env=env, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    return None


def _parse_stamp(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------- checkpoints

def _rollback_check(root: str) -> Check:
    version = _git_version()
    if version is None:
        return Check("rollback", SKIP, "rollback checkpoints",
                     "Git is not installed; checkpoints and rollback are unavailable")
    try:
        repo = _rollback.Repository(root)
        checkpoints = repo.checkpoints()
    except (_rollback.CheckpointError, OSError) as exc:
        return Check("rollback", INFO, "rollback checkpoints",
                     f"{version}; {root} is not a Git work tree ({_one_line(exc)})")
    count = len(checkpoints)
    detail = f"{version}; {count} checkpoint" + ("" if count == 1 else "s")
    if checkpoints:
        try:
            files, total, largest = repo.snapshot_stats(checkpoints[0].tree)
            newest = f"newest tree {total} bytes in {files} file(s)"
            if largest[0]:
                newest += f", largest {largest[0]} ({largest[1]} bytes)"
            detail = f"{detail} ({newest})"
        except (_rollback.CheckpointError, OSError, ValueError, TypeError):
            detail = f"{detail} (the newest tree size could not be read)"
    if count > CHECKPOINT_WARN_COUNT:
        return Check("rollback", WARN, "rollback checkpoints",
                     f"{detail}: more than {CHECKPOINT_WARN_COUNT} checkpoints hold disk space",
                     fix="Run `runledger checkpoints prune --keep 50` to drop the oldest refs "
                         "(the objects are reclaimed by a later `git gc`).")
    return Check("rollback", OK, "rollback checkpoints", detail)


def _git_version() -> Optional[str]:
    if not shutil.which("git"):
        return None
    try:
        proc = subprocess.run(["git", "--version"], capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    text = _decode(proc.stdout).strip() or "git"
    match = re.search(r"\d+\.\d+(?:\.\d+)*", text)
    return f"git {match.group(0)}" if match else text


# ---------------------------------------------------------------- pricing

def _pricing_check(today: Optional[date] = None) -> Check:
    path = pricing.prices_path()
    override = ("Add model prices to ~/.runledger/prices.json or set RUNLEDGER_PRICES "
                "(see docs/agents.md); bundled prices are re-verified periodically.")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return Check("pricing", FAIL, "pricing",
                     f"the bundled {path} cannot be read: {_one_line(exc)}", fix=override)
    try:
        data = json.loads(text)
    except ValueError as exc:
        return Check("pricing", FAIL, "pricing", f"{path} is not valid JSON: {exc}", fix=override)
    try:
        pricing._parse_table(data, str(path))   # a broken row makes the whole table unusable
    except ValueError as exc:
        return Check("pricing", FAIL, "pricing", f"{path} is unusable: {exc}", fix=override)
    checked = pricing.parse_checked_date(data.get("checked") if isinstance(data, dict) else None)
    if checked is None:
        return Check("pricing", INFO, "pricing",
                     f"{path} carries no usable `checked` date, so its age cannot be told")
    age = ((today or date.today()) - checked).days
    if age > pricing.STALE_DAYS:
        return Check("pricing", WARN, "pricing",
                     f"prices.json was checked on {checked.isoformat()} ({age} days ago), older "
                     f"than the {pricing.STALE_DAYS}-day limit: costs may be stale",
                     fix=override)
    return Check("pricing", OK, "pricing",
                 f"prices.json checked {checked.isoformat()} ({age} days ago)")


# ---------------------------------------------------------------- sessions

def _sessions_checks(root: str, agents: Optional[Sequence[str]]) -> List[Check]:
    from . import adapters

    selected = [a for a in SESSION_AGENTS if not agents or a in set(agents)]
    out: List[Check] = []
    with _no_session_cache():
        for agent in selected:
            home = _agent_home(agent, root)
            try:
                found = adapters.get(agent).find_sessions(root)
            except KeyError:
                out.append(Check("sessions", INFO, "sessions",
                                 f"{agent}: the adapter could not be loaded", agent=agent))
                continue
            except Exception as exc:  # a broken transcript must not stop the report
                out.append(Check("sessions", WARN, "sessions",
                                 f"{agent}: session discovery failed ({_one_line(exc)})",
                                 fix="Check the session files the agent writes.", agent=agent))
                continue
            out.append(Check("sessions", INFO, "sessions",
                             f"{agent}: {len(found)} session(s) for this project (home {home})",
                             agent=agent))
    return out


def _agent_home(agent: str, root: str) -> str:
    """The folder session discovery reads for that agent, so the report can be checked."""
    if agent == AGENT_CLAUDE:
        return str(hook_targets.claude_dir())
    if agent == AGENT_CODEX:
        return str(hook_targets.codex_dir())
    if agent == AGENT_DROID:
        return str(hook_targets.factory_dir())
    return root


@contextlib.contextmanager
def _no_session_cache():
    """Discovery must not write the shared header cache: `doctor` is read-only."""
    name = "RUNLEDGER_NO_SESSION_CACHE"
    previous = os.environ.get(name)
    os.environ[name] = "1"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = previous


# ---------------------------------------------------------------- guard mode

def _mode_check(user: Dict[str, Any], project_policy: Dict[str, Any]) -> Check:
    cfg, _ = _guard._effective_guard(project_policy, user,
                                     _guard.trusted_approval_settings(user))
    return Check("guard.mode", INFO, "guard mode",
                 f"mode {cfg['mode']}; fail_closed {'on' if cfg['fail_closed'] else 'off'}")


# ---------------------------------------------------------------- rendering

def render_text(report: Report, verbose: bool = False) -> str:
    """The text report: one line per check, `->` fix hints, a final summary line."""
    from . import __version__

    lines = [f"RunLedger doctor {__version__}  |  python {platform.python_version()}  |  "
             f"{sys.platform}  |  {report.project}"]
    for check in report.checks:
        who = f" {check.agent}" if check.agent else ""
        who += f" {check.scope}" if check.scope else ""
        lines.append(f"[{check.status}] {check.title}{who}: {check.detail}")
        if check.fix and check.status in (WARN, FAIL):
            lines.append(f"        -> {check.fix}")
        if verbose:
            lines.extend(f"        {extra}" for extra in check_extra(report, check))
    counts = summary(report.checks)
    lines.append(f"Summary: {counts['ok']} ok, {counts['warn']} warnings, {counts['fail']} failures")
    return "\n".join(lines) + "\n"


def check_extra(report: Report, check: Check) -> List[str]:
    """The `--verbose` lines behind one check: the files and the environment it read.

    Only names and paths, never a value: `--verbose` must not print a secret either."""
    if check.id == "install":
        return [f"python: {sys.executable}",
                f"PATH lookup: runledger -> {shutil.which('runledger') or 'not found'}"]
    if check.id == "guard.hooks" and check.agent and check.scope:
        path = hook_targets.target_for(check.agent, report.project,
                                       global_scope=(check.scope == "global")).path
        return [f"hook file: {path} (exists: {Path(path).exists()})"]
    if check.id == "guard.note":
        return ["the trust/restart state lives inside the agent and cannot be read here"]
    if check.id == "guard.selftest":
        return ["isolation: temp cwd, temp HOME, no approval server, no API key"]
    if check.id == "config":
        return [f"user config: {user_config_path()}",
                f"project file: {Path(report.project) / PROJECT_POLICY_FILE}"]
    if check.id in ("approval", "log", "rollback", "pricing"):
        return _file_extras(check, report)
    if check.id == "sessions":
        return [f"counts only; no transcript is read into the report"]
    return []


def _file_extras(check: Check, report: Report) -> List[str]:
    if check.id == "approval":
        trusted = _guard.trusted_approval_settings(_guard.load_user_config())
        return [f"server from the environment or the user file: "
                f"{trusted['server'] or 'not configured'}",
                f"health url: {(trusted['server'] or '<none>').rstrip('/')}/health",
                f"key variable: {trusted['api_key_env']} (only set/not set is shown)"]
    if check.id == "log":
        return [f"log file: {Path(report.project) / _guard.LOG_DIR / _guard.LOG_FILE}",
                "the log's rows are counted, never printed"]
    if check.id == "rollback":
        return [f"checkpoint refs: {_rollback.PREFIX}"]
    return [f"prices: {pricing.prices_path()}"]


def render_json(report: Report) -> Dict[str, Any]:
    from . import __version__

    return {"version": JSON_VERSION, "runledger": __version__,
            "project": report.project, "summary": summary(report.checks),
            "checks": [c.as_dict() for c in report.checks]}


def _emit(text: str) -> None:
    stream = sys.stdout
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is not None:
        try:
            reconfigure(errors="replace")
        except (ValueError, OSError):
            pass
    stream.write(text)
    stream.flush()


def cmd_doctor(args) -> int:
    """`runledger doctor`: the nine checks, text or `--json`, exit code 0/1/2."""
    report = run_checks(project=getattr(args, "project", None),
                        agents=getattr(args, "agent", None),
                        offline=getattr(args, "offline", False),
                        selftest=not getattr(args, "no_selftest", False))
    if getattr(args, "as_json", False):
        _emit(json.dumps(render_json(report), indent=2) + "\n")
    else:
        _emit(render_text(report, verbose=getattr(args, "verbose", False)))
    return exit_code(report.checks)


def cmd_guard_status(args) -> int:
    """`runledger guard status`: the guard-only view, text or `--json`, exit code 0/1/2."""
    report = run_guard_status(project=getattr(args, "project", None),
                              agents=getattr(args, "agent", None))
    if getattr(args, "as_json", False):
        _emit(json.dumps(render_json(report), indent=2) + "\n")
    else:
        _emit(render_text(report))
    return exit_code(report.checks)
