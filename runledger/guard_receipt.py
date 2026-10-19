"""Read local Guard decisions without loading the project's whole JSONL log."""
from __future__ import annotations

import json
import os
import re
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from . import gitinfo
from .guard import LOG_DIR, LOG_FILE, _summary, project_root, redact
from .parser import Run, Step
from .paths import is_unc


# Guard writes short records. Bound memory even for a corrupt, enormous single line.
_MAX_LINE = 64 * 1024
_USER_REJECTION = re.compile(
    r"^(?:<tool_use_error>\s*)?(?:error:\s*)?(?:"
    r"the user (?:doesn't|does not) want to proceed with this tool use\b|"
    r"user (?:rejected|denied) (?:the |this )?tool (?:use|call|request)\b|"
    r"tool (?:use|call|request) (?:was )?(?:rejected|denied) by (?:the )?user\b|"
    r"permission to use .+ (?:has been|was) denied\b|"
    r"request interrupted by user\b)",
    re.IGNORECASE,
)
# One shell call, two names: the adapters build a `PowerShell` step when the recorded
# command runs powershell, while Droid and Codex report the tool as `Execute` or `Bash`.
_SHELL_TOOLS = ("Bash", "PowerShell")


def _records(path: Path) -> Iterator[Dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            while True:
                line = fh.readline(_MAX_LINE + 1)
                if not line:
                    break
                if len(line) > _MAX_LINE:
                    while line and not line.endswith("\n"):
                        line = fh.readline(_MAX_LINE + 1)
                    continue
                try:
                    record = json.loads(line)
                except (ValueError, RecursionError):
                    continue
                if isinstance(record, dict):
                    yield record
    except (OSError, ValueError):
        return  # missing, unreadable or invalid cwd: the receipt still works


def _outcome(record: Dict[str, Any], step: Optional[Step]) -> Optional[str]:
    decision = record.get("decision")
    policy = record.get("policy_decision", decision)
    if record.get("mode") == "monitor":
        if policy == "deny":
            return "Would have been blocked (monitor mode)"
        if policy == "ask":
            return "Would have been asked (monitor mode)"
        return None
    if record.get("approval") == "approved":
        return "Approved by approval server"
    if record.get("approval") == "denied":
        return "Denied by approval server"
    if decision == "deny":
        return "Blocked by Guard"
    if decision == "ask":
        if (step is None or not (step.has_result or step.result_text)
                or _USER_REJECTION.match(step.result_text.strip())):
            return "Rejected by user"
        return "Approved by user"
    return None  # plain allows, warnings and errors aren't decisions to display


def _log_paths(cwd: str) -> List[Path]:
    """Candidate guard.log files for a run: project root, git top-level, and run.cwd."""
    candidates = [project_root(cwd), gitinfo.toplevel(cwd), cwd]
    paths: List[Path] = []
    seen = set()
    for d in candidates:
        if not d or is_unc(d):
            continue
        p = Path(d) / LOG_DIR / LOG_FILE
        try:
            key = os.path.normcase(os.path.abspath(str(p)))
        except OSError:
            key = str(p)
        if key not in seen:
            seen.add(key)
            paths.append(p)
    return paths


def _take_step(by_summary: Dict[Any, "deque"], tools: List[str], text: str,
               used: set) -> Optional[Step]:
    """The oldest unconsumed step carrying `text` among `tools`, in transcript order.

    The Bash and PowerShell queues are compared by step index, not by the order of `tools`:
    a repeated command that switched shells keeps the order it ran in."""
    best: Optional[Step] = None
    for tool in tools:
        queue = by_summary.get((tool, text))
        while queue and queue[0].index in used:
            queue.popleft()
        if queue and (best is None or queue[0].index < best.index):
            best = queue[0]
    if best is None:
        return None
    queue = by_summary[(best.tool, text)]
    while queue and queue[0].index in used:
        queue.popleft()
    if queue:
        queue.popleft()
    return best


def load_guard_decisions(run: Run) -> List[Dict[str, Any]]:
    """Match by tool id, then by the Guard's own tool + summary for the rows ids cannot explain.

    A row whose recorded id the transcript knows takes that step first, whatever its summary;
    every other row - a legacy record without an id, a Droid row (whose log never keeps the
    id), or a Codex id the rollout does not carry - is matched on the tool name and summary the
    Guard logged. Repeated calls consume steps in transcript order, including blocked calls.
    A record whose tool name the agent's vocabulary translated matches the step under the
    canonical name (Droid logs `Execute`, the receipt step is `Bash`); a row that names its
    agent may also cross `Bash`/`PowerShell`, which the adapters choose by the recorded
    program. Reads from project root, git top-level, and `run.cwd` (legacy subfolder
    location), de-duplicating identical entries across files.
    """
    if not run.cwd or is_unc(run.cwd):
        return []
    by_id = {s.tool_use_id: s for s in run.steps if s.tool_use_id}
    by_summary = defaultdict(deque)
    for step in run.steps:
        by_summary[(step.tool, _summary(step.tool, step.input))].append(step)
    used: set = set()
    seen_keys = set()
    rows: List[Tuple[int, Dict[str, Any]]] = []
    for log_idx, log_path in enumerate(_log_paths(run.cwd)):
        seen_in_file = set()
        for record in _records(log_path):
            if record.get("session_id") != run.session_id:
                continue
            if record.get("decision") not in ("deny", "ask", "allow"):
                continue
            tool, summary = record.get("tool"), record.get("summary", "")
            if not isinstance(tool, str) or not isinstance(summary, str):
                continue
            dedup_key = (record.get("ts"), tool, record.get("decision"), summary)
            if record.get("ts") is not None:
                if dedup_key in seen_keys:
                    continue
                seen_keys.add(dedup_key)
            else:
                if log_idx > 0 and dedup_key in seen_keys:
                    continue
                seen_in_file.add(dedup_key)
            # Filter non-interventions before consuming a potential step.
            if _outcome(record, None) is None:
                continue
            rows.append((len(rows), record))
        seen_keys.update(seen_in_file)
    matched: Dict[int, Optional[Step]] = {}
    for index, record in rows:
        tool_id = record.get("tool_use_id")
        step = by_id.get(tool_id) if isinstance(tool_id, str) and tool_id else None
        if step is not None and step.index not in used:
            used.add(step.index)
            matched[index] = step
    for index, record in rows:
        if index in matched:
            continue
        canonical = record.get("canonical_tool")
        match_tool = canonical if isinstance(canonical, str) and canonical else record["tool"]
        tools = [match_tool]
        if isinstance(record.get("agent"), str) and match_tool in _SHELL_TOOLS:
            tools += [t for t in _SHELL_TOOLS if t != match_tool]
        step = _take_step(by_summary, tools, redact(record["summary"]), used)
        if step is not None:
            used.add(step.index)
        matched[index] = step
    decisions: List[Dict[str, Any]] = []
    for index, record in rows:
        tool, summary = record["tool"], record.get("summary", "")
        step = matched.get(index)
        codes = record.get("codes")
        decisions.append({
                "step": step.index if step is not None else None,
                "tool": redact(tool), "summary": redact(summary),
                "decision": record["decision"], "outcome": _outcome(record, step),
                "reason": redact(record.get("reason") or ""),
                "codes": [redact(c) for c in codes if isinstance(c, str)]
                if isinstance(codes, list) else [],
                "ts": redact(record["ts"]) if isinstance(record.get("ts"), str) else None,
            })
    return decisions


def guard_warning_count(run: Run) -> int:
    """Rows the Guard logged as a plain warning for this run (an ignored loosening setting).
    Monitor-mode rows are decisions, which `guard_counts` already reports, so they are not
    counted here. Missing, unreadable and unknown-CWD logs count zero."""
    if not run.cwd or is_unc(run.cwd):
        return 0
    total = 0
    seen_files = set()
    seen = set()
    for path in _log_paths(run.cwd):
        try:
            key = os.path.normcase(os.path.abspath(str(path)))
        except OSError:
            key = str(path)
        if key in seen_files:
            continue
        seen_files.add(key)
        for record in _records(path):
            if record.get("session_id") != run.session_id:
                continue
            if record.get("decision") != "warning" or record.get("mode") == "monitor":
                continue  # monitor-mode rows are decisions, and guard_counts() counts them
            dup = (record.get("ts"), record.get("reason"))
            if record.get("ts") is not None:
                if dup in seen:
                    continue
                seen.add(dup)
            total += 1
    return total


def guard_counts(decisions: List[Dict[str, Any]]) -> Dict[str, int]:
    outcomes = [d["outcome"] for d in decisions]
    approved = sum(o in ("Approved by user", "Approved by approval server") for o in outcomes)
    rejected = sum(o in ("Rejected by user", "Denied by approval server") for o in outcomes)
    return {
        "blocked": outcomes.count("Blocked by Guard"),
        "asked": approved + rejected,
        "approved": approved, "rejected": rejected,
        "monitor_blocked": outcomes.count("Would have been blocked (monitor mode)"),
        "monitor_asked": outcomes.count("Would have been asked (monitor mode)"),
    }


def guard_summary(decisions: List[Dict[str, Any]]) -> str:
    counts = guard_counts(decisions)
    parts = []
    if counts["blocked"]:
        parts.append(f'{counts["blocked"]} blocked')
    if counts["asked"]:
        resolved = f'{counts["approved"]} approved'
        if counts["rejected"]:
            resolved += f', {counts["rejected"]} rejected'
        parts.append(f'{counts["asked"]} asked ({resolved})')
    if counts["monitor_blocked"]:
        parts.append(f'{counts["monitor_blocked"]} would be blocked (monitor mode)')
    if counts["monitor_asked"]:
        parts.append(f'{counts["monitor_asked"]} would be asked (monitor mode)')
    return ", ".join(parts)
