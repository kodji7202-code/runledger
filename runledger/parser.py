"""Parse Claude Code session transcripts (JSONL) into a structured Run.

Claude Code stores one JSONL file per session under
~/.claude/projects/<encoded-project-path>/<session-id>.jsonl.
Each line is an event. Assistant events carry `message` with `model`,
`usage` and `content` blocks (text / thinking / tool_use). User events carry
the human prompt or `tool_result` blocks answering earlier tool calls.

Streaming can split one assistant message over several lines that share the
same `message.id` and repeat the same `usage`, so usage is counted once per
message id.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0

    @property
    def total(self) -> int:
        return (self.input_tokens + self.output_tokens
                + self.cache_write_tokens + self.cache_read_tokens)

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_write_tokens += other.cache_write_tokens
        self.cache_read_tokens += other.cache_read_tokens


def _split_usage(usage: Usage, count: int) -> List[Usage]:
    """Split usage across `count` steps without losing integer remainders."""
    parts = [Usage() for _ in range(count)]
    for field_name in ("input_tokens", "output_tokens", "cache_write_tokens", "cache_read_tokens"):
        quotient, remainder = divmod(getattr(usage, field_name), count)
        for i, part in enumerate(parts):
            setattr(part, field_name, quotient + (1 if i < remainder else 0))
    return parts


@dataclass
class Step:
    index: int
    tool: str
    input: Dict[str, Any]
    tool_use_id: str
    model: Optional[str]
    timestamp: Optional[str]
    usage: Usage = field(default_factory=Usage)   # allocated share of the assistant message that issued it
    cost: Optional[float] = None
    result_text: str = ""
    is_error: bool = False
    summary: str = ""
    risks: List["Risk"] = field(default_factory=list)  # filled by risk module
    has_result: bool = False  # an empty tool_result still confirms the call ran
    usage_estimated: bool = False  # usage is an even share of a session total, not a per-call count
    usage_known: bool = True  # False when no source attributed usage to this step; usage is then 0, not real


@dataclass
class Run:
    session_id: str
    path: str
    cwd: Optional[str]
    git_branch: Optional[str]
    started: Optional[str]
    ended: Optional[str]
    prompts: List[str]
    steps: List[Step]
    final_message: str
    usage: Usage
    models: Dict[str, Usage]
    cost: Optional[float] = None
    overall_summary: str = ""
    agent: str = "claude-code"            # adapter NAME that produced this run
    reported_cost: Optional[float] = None  # cost reported by the agent itself, if any
    # Phase 5 analysis results (see docs/analysis.md for the schemas):
    quality: Optional[Dict[str, Any]] = None          # quality.py: {"score","grade","signals":[...]}
    recommendations: List[Dict[str, Any]] = field(default_factory=list)  # advisor.py
    ai_review: Optional[Dict[str, Any]] = None        # review.py (Claude Sonnet): verdict + assessments
    checkpoint_id: Optional[str] = None              # earliest local checkpoint for this session
    guard: List[Dict[str, Any]] = field(default_factory=list)  # local Guard decisions
    # gitinfo.working_changes for files the run wrote through the shell, keyed relative to cwd
    git_changes: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # gitinfo.file_diffs for changed test files, keyed relative to cwd; read by testdiff, never shown
    test_diffs: Dict[str, str] = field(default_factory=dict)
    # Droid only: "messages", "settings" or "missing"; None for adapters with usage in every message
    usage_source: Optional[str] = None
    usage_file: Optional[str] = None  # file name (no folder) of the settings file that supplied usage

    @property
    def duration_seconds(self) -> Optional[float]:
        a, b = _ts(self.started), _ts(self.ended)
        if a and b:
            return max(0.0, (b - a).total_seconds())
        return None


_LONG_FRACTION = re.compile(r"(\.\d{6})\d+")


def _ts(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO timestamp as UTC; naive values are taken as UTC so they compare with aware ones."""
    if not value or not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "+00:00").replace("z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        # Python 3.9 and 3.10 reject more than 6 fractional digits (nanosecond stamps).
        try:
            parsed = datetime.fromisoformat(_LONG_FRACTION.sub(r"\1", text))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _count(value: Any) -> int:
    """A token count from untrusted JSON: non-numbers, negatives, NaN and booleans count as 0."""
    if isinstance(value, bool):
        return 0
    try:
        number = int(float(value)) if isinstance(value, str) else int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, number)


def _usage(raw: Optional[Dict[str, Any]]) -> Usage:
    raw = raw if isinstance(raw, dict) else {}
    return Usage(
        input_tokens=_count(raw.get("input_tokens")),
        output_tokens=_count(raw.get("output_tokens")),
        cache_write_tokens=_count(raw.get("cache_creation_input_tokens")),
        cache_read_tokens=_count(raw.get("cache_read_input_tokens")),
    )


def _text_of(content: Any, depth: int = 0) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list) and depth < 20:
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    text = block.get("text")
                    parts.append(text if isinstance(text, str) else "")
                elif "content" in block:
                    parts.append(_text_of(block["content"], depth + 1))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(p for p in parts if p)
    return ""


_EVENT_TYPES = {"user", "assistant", "system", "summary", "result", "file-history-snapshot",
                "queue-operation", "progress"}


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _is_event(ev: Dict[str, Any]) -> bool:
    return _str(ev.get("type")) in _EVENT_TYPES or isinstance(ev.get("sessionId"), str)


def parse_session(path: str | os.PathLike) -> Run:
    path = str(path)
    events: List[Dict[str, Any]] = []
    lines = 0
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            lines += 1
            try:
                event = json.loads(line)
            except (ValueError, RecursionError):
                continue  # tolerate partial lines from a live session, and absurdly deep nesting
            if isinstance(event, dict):
                events.append(event)
    if lines and not any(_is_event(ev) for ev in events):
        raise ValueError(f"{path}: not a supported session file")

    session_id = Path(path).stem
    cwd = git_branch = started = ended = None
    prompts: List[str] = []
    steps: List[Step] = []
    by_tool_id: Dict[str, Step] = {}
    seen_msg_ids: Dict[str, List[Step]] = {}
    msg_usage: Dict[str, Usage] = {}
    msg_model: Dict[str, Optional[str]] = {}
    final_message = ""

    for ev in events:
        etype = ev.get("type")
        ts = _str(ev.get("timestamp")) or None
        if ts:
            started = started or ts
            ended = ts
        cwd = cwd or _str(ev.get("cwd")) or None
        git_branch = git_branch or _str(ev.get("gitBranch")) or None
        session_id = _str(ev.get("sessionId")) or session_id
        msg = ev.get("message")
        msg = msg if isinstance(msg, dict) else {}

        if etype == "user":
            content = msg.get("content")
            if isinstance(content, str):
                if not ev.get("isMeta") and content.strip() and not content.startswith("<command-"):
                    prompts.append(content.strip())
                continue
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_result":
                    step = by_tool_id.get(_str(block.get("tool_use_id")))
                    if step:
                        step.result_text = _text_of(block.get("content"))[:4000]
                        step.is_error = bool(block.get("is_error"))
                        step.has_result = True
                elif block.get("type") == "text" and not ev.get("isMeta"):
                    t = _str(block.get("text")).strip()
                    if t and not t.startswith("<"):
                        prompts.append(t)

        elif etype == "assistant":
            mid = _str(msg.get("id")) or _str(ev.get("uuid")) or str(len(seen_msg_ids))
            model = _str(msg.get("model")) or None
            if model == "<synthetic>":
                model = None
            if mid not in msg_usage:
                msg_usage[mid] = _usage(msg.get("usage"))
                msg_model[mid] = model
                seen_msg_ids[mid] = []
            else:
                # later chunks may carry the final (larger) usage figures
                u = _usage(msg.get("usage"))
                if u.total > msg_usage[mid].total:
                    msg_usage[mid] = u
            content = msg.get("content")
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    inp = block.get("input")
                    step = Step(
                        index=len(steps) + 1,
                        tool=_str(block.get("name")) or "?",
                        input=inp if isinstance(inp, dict) else {},
                        tool_use_id=_str(block.get("id")),
                        model=model,
                        timestamp=ts,
                    )
                    steps.append(step)
                    by_tool_id[step.tool_use_id] = step
                    seen_msg_ids[mid].append(step)
                elif block.get("type") == "text" and _str(block.get("text")).strip():
                    final_message = block["text"].strip()

    # Attribute each assistant message's usage to the steps it issued
    # (split evenly); messages without tool calls count toward the run only.
    total = Usage()
    models: Dict[str, Usage] = {}
    for mid, u in msg_usage.items():
        total.add(u)
        m = msg_model.get(mid) or "unknown"
        models.setdefault(m, Usage()).add(u)
        issued = seen_msg_ids.get(mid) or []
        if issued:
            for s, share in zip(issued, _split_usage(u, len(issued))):
                s.usage = share

    return Run(
        session_id=session_id, path=path, cwd=cwd, git_branch=git_branch,
        started=started, ended=ended, prompts=prompts, steps=steps,
        final_message=final_message, usage=total, models=models,
    )


def claude_projects_dir() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(base) / "projects" if base else Path.home() / ".claude" / "projects"


_WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:[\\/]")


def encode_project_path(project: str) -> str:
    """Claude Code names project folders by replacing every non-alphanumeric
    character of the project path with '-': '/home/dev/my.app' becomes
    '-home-dev-my-app' and 'D:\\runledger' becomes 'D--runledger'.

    Paths that are already absolute (POSIX or Windows form) are encoded as
    given; only relative paths are resolved against the current directory."""
    if not (project.startswith("/") or _WINDOWS_ABSOLUTE.match(project)):
        project = os.path.abspath(project)
    return "".join(ch if ch.isascii() and ch.isalnum() else "-" for ch in project)


def find_sessions(project: Optional[str] = None) -> List[Path]:
    root = claude_projects_dir()
    if not root.exists():
        return []
    if project:
        d = root / encode_project_path(project)
        files = list(d.glob("*.jsonl")) if d.exists() else []
    else:
        files = list(root.glob("*/*.jsonl"))
    return sorted(files, key=lambda f: f.stat().st_mtime, reverse=True)
