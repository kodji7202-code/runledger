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
from dataclasses import dataclass, field
from datetime import datetime
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


@dataclass
class Step:
    index: int
    tool: str
    input: Dict[str, Any]
    tool_use_id: str
    model: Optional[str]
    timestamp: Optional[str]
    usage: Usage = field(default_factory=Usage)   # usage of the assistant message that issued it (shared share)
    cost: Optional[float] = None
    result_text: str = ""
    is_error: bool = False
    summary: str = ""
    risks: List["Risk"] = field(default_factory=list)  # filled by risk module


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

    @property
    def duration_seconds(self) -> Optional[float]:
        a, b = _ts(self.started), _ts(self.ended)
        if a and b:
            return max(0.0, (b - a).total_seconds())
        return None


def _ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _usage(raw: Optional[Dict[str, Any]]) -> Usage:
    raw = raw or {}
    return Usage(
        input_tokens=int(raw.get("input_tokens") or 0),
        output_tokens=int(raw.get("output_tokens") or 0),
        cache_write_tokens=int(raw.get("cache_creation_input_tokens") or 0),
        cache_read_tokens=int(raw.get("cache_read_input_tokens") or 0),
    )


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif "content" in block:
                    parts.append(_text_of(block["content"]))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(p for p in parts if p)
    return ""


def parse_session(path: str | os.PathLike) -> Run:
    path = str(path)
    events: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # tolerate partial lines from a live session

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
        ts = ev.get("timestamp")
        if ts:
            started = started or ts
            ended = ts
        cwd = cwd or ev.get("cwd")
        git_branch = git_branch or ev.get("gitBranch")
        session_id = ev.get("sessionId") or session_id
        msg = ev.get("message") or {}

        if etype == "user":
            content = msg.get("content")
            if isinstance(content, str):
                if not ev.get("isMeta") and content.strip() and not content.startswith("<command-"):
                    prompts.append(content.strip())
                continue
            for block in content or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_result":
                    step = by_tool_id.get(block.get("tool_use_id", ""))
                    if step:
                        step.result_text = _text_of(block.get("content"))[:4000]
                        step.is_error = bool(block.get("is_error"))
                elif block.get("type") == "text" and not ev.get("isMeta"):
                    t = block.get("text", "").strip()
                    if t and not t.startswith("<"):
                        prompts.append(t)

        elif etype == "assistant":
            mid = msg.get("id") or ev.get("uuid") or str(len(seen_msg_ids))
            model = msg.get("model")
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
            for block in msg.get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    step = Step(
                        index=len(steps) + 1,
                        tool=block.get("name", "?"),
                        input=block.get("input") or {},
                        tool_use_id=block.get("id", ""),
                        model=model,
                        timestamp=ts,
                    )
                    steps.append(step)
                    by_tool_id[step.tool_use_id] = step
                    seen_msg_ids[mid].append(step)
                elif block.get("type") == "text" and block.get("text", "").strip():
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
            n = len(issued)
            share = Usage(u.input_tokens // n, u.output_tokens // n,
                          u.cache_write_tokens // n, u.cache_read_tokens // n)
            for s in issued:
                s.usage = share

    return Run(
        session_id=session_id, path=path, cwd=cwd, git_branch=git_branch,
        started=started, ended=ended, prompts=prompts, steps=steps,
        final_message=final_message, usage=total, models=models,
    )


def claude_projects_dir() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(base) / "projects" if base else Path.home() / ".claude" / "projects"


def encode_project_path(project: str) -> str:
    """Claude Code names project folders by replacing path separators (and
    other non-alphanumerics) with '-'."""
    p = os.path.abspath(project)
    return "".join(ch if ch.isalnum() else "-" for ch in p)


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
