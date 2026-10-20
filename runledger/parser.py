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
from typing import Any, Dict, List, Optional, Tuple

# How a path a parse read or looked for beside a transcript turned out: it was read, it was not
# there, or it was there and could not be used. `runledger stats` records the state beside the
# path's size and modification time, so its cache record is dropped when a candidate appears
# (a Droid settings sidecar written later), is repaired (a child settings file that was
# malformed) or changes (a sub-agent transcript filled in).
SOURCE_OK = "ok"
SOURCE_MISSING = "missing"
SOURCE_REJECTED = "rejected"

# A Claude sub-agent folder tree larger than this is watched at its shallowest folders only.
# The cap is a speed guard, and a tree that reaches it is never cached: a transcript written
# into a folder the watch did not reach changes no watched source, so a stored record would
# keep totals `--no-cache` disproves (LOCAL-R3-STATS-01).
_MAX_SUBAGENT_FOLDERS = 64


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_1h_tokens: int = 0
    _requests: List["Usage"] = field(default_factory=list, init=False, repr=False, compare=False)
    _request_prompt_tokens: Optional[int] = field(default=None, init=False, repr=False, compare=False)
    _cumulative_only: bool = field(default=False, init=False, repr=False, compare=False)

    @property
    def total(self) -> int:
        return (self.input_tokens + self.output_tokens
                + max(self.cache_write_tokens, self.cache_write_1h_tokens)
                + self.cache_read_tokens)

    @property
    def prompt_tokens(self) -> int:
        return (self.input_tokens + self.cache_read_tokens
                + max(self.cache_write_tokens, self.cache_write_1h_tokens))

    def add(self, other: "Usage") -> None:
        if self.total > 0 and not self._requests:
            seed = Usage(
                self.input_tokens,
                self.output_tokens,
                self.cache_write_tokens,
                self.cache_read_tokens,
                self.cache_write_1h_tokens,
            )
            seed._request_prompt_tokens = self._request_prompt_tokens
            seed._cumulative_only = self._cumulative_only
            self._requests.append(seed)
        if other._requests:
            self._requests.extend(other._requests)
        elif other.total > 0:
            snap = Usage(
                other.input_tokens,
                other.output_tokens,
                other.cache_write_tokens,
                other.cache_read_tokens,
                other.cache_write_1h_tokens,
            )
            snap._request_prompt_tokens = other._request_prompt_tokens
            snap._cumulative_only = other._cumulative_only
            self._requests.append(snap)
        if other._cumulative_only:
            self._cumulative_only = True
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_write_tokens += other.cache_write_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_write_1h_tokens += other.cache_write_1h_tokens


def _split_usage(usage: Usage, count: int) -> List[Usage]:
    """Split usage across `count` steps without losing integer remainders."""
    parts = [Usage() for _ in range(count)]
    req_prompt = (usage._request_prompt_tokens
                  if usage._request_prompt_tokens is not None else usage.prompt_tokens)
    for part in parts:
        part._request_prompt_tokens = req_prompt
        part._cumulative_only = usage._cumulative_only
    for field_name in ("input_tokens", "output_tokens", "cache_write_tokens",
                       "cache_read_tokens", "cache_write_1h_tokens"):
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
    cost_partial: bool = False  # cost covers only the priced models; others had tokens but no price
    overall_summary: str = ""
    agent: str = "claude-code"            # adapter NAME that produced this run
    reported_cost: Optional[float] = None  # cost reported by the agent itself, if any
    # Phase 5 analysis results (see docs/analysis.md for the schemas):
    quality: Optional[Dict[str, Any]] = None          # quality.py: {"score","grade","signals":[...]}
    recommendations: List[Dict[str, Any]] = field(default_factory=list)  # advisor.py
    ai_review: Optional[Dict[str, Any]] = None        # review.py (Claude Sonnet): verdict + assessments
    checkpoint_id: Optional[str] = None              # earliest local checkpoint for this session
    guard: List[Dict[str, Any]] = field(default_factory=list)  # local Guard decisions
    notes: List[str] = field(default_factory=list)   # policy / trust / receipt notes
    # gitinfo.working_changes for files the run wrote through the shell, keyed relative to cwd
    git_changes: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # gitinfo.file_diffs for changed test files, keyed relative to cwd; read by testdiff, never shown
    test_diffs: Dict[str, str] = field(default_factory=dict)
    # testdiff.analyze verdicts (testdiff.TestChange values), computed once by risk.assess and
    # read by quality.score_run, so one build never diffs the test files twice
    test_verdicts: Optional[List[Any]] = None
    # gitinfo.session_base: short sha the Git facts were compared with, None when that was HEAD
    git_base: Optional[str] = None
    # Droid only: "messages", "settings" or "missing"; None for adapters with usage in every message
    usage_source: Optional[str] = None
    usage_file: Optional[str] = None  # file name (no folder) of the settings file that supplied usage
    # Droid only: where cwd came from. "header" (the log), "session" (`% pwd` output or the tool
    # paths' Git work tree), "settings", "session_dir" (the encoded session folder) or "unknown"
    cwd_source: Optional[str] = None
    # Every path this parse read or looked for beside the transcript - a Droid settings file or
    # child settings file, each folder a Claude sub-agent transcript could live in, and the
    # transcripts themselves - as `(path, state)`: the state is `SOURCE_OK`, `SOURCE_MISSING`
    # or `SOURCE_REJECTED`. `runledger stats` keys its cache record on all of them, so one that
    # appears, is repaired or changes drops the record.
    sources: List[Tuple[str, str]] = field(default_factory=list)
    # Whether the parse really could track every path it depends on, so nothing it read was cut
    # short: False when a dependency tree could not be listed or its folder watch was capped.
    # `runledger stats` then caches no record for the session - a source that was never watched
    # cannot invalidate one - and parses it again on every run instead (LOCAL-R3-STATS-01).
    dependencies_complete: bool = True
    safe: bool = False  # safe.make_safe rewrote paths and masked e-mails for sharing
    # safe.make_safe: the changed files computed before paths were rewritten (summarize.FileChange values)
    frozen_changes: Optional[Dict[str, Any]] = None
    # receipt.redact_run: the whole-path labels of the raw commands, by step index. Masking a
    # command takes a spaced drive path apart, so the labels a receipt shows are read before it.
    show_labels: Optional[Dict[int, Dict[str, str]]] = None

    @property
    def duration_seconds(self) -> Optional[float]:
        a, b = _ts(self.started), _ts(self.ended)
        if a and b:
            return max(0.0, (b - a).total_seconds())
        return None


_FRACTION = re.compile(r"\.(\d+)")


def _ts(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO timestamp as UTC; naive values are taken as UTC so they compare with aware ones."""
    if not value or not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "+00:00").replace("z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        # Python 3.9 and 3.10 accept only 3 or 6 fractional digits (not .5 or nanoseconds).
        try:
            parsed = datetime.fromisoformat(
                _FRACTION.sub(lambda m: "." + m.group(1)[:6].ljust(6, "0"), text, count=1))
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
    cc = raw.get("cache_creation")
    cc = cc if isinstance(cc, dict) else {}
    cw_1h = _count(cc.get("ephemeral_1h_input_tokens"))
    cw_5m = _count(cc.get("ephemeral_5m_input_tokens"))
    cw_total = _count(raw.get("cache_creation_input_tokens"))
    if cw_total == 0 and (cw_1h or cw_5m):
        cw_total = cw_5m + cw_1h
    elif cw_1h > cw_total:
        cw_total = cw_1h
    return Usage(
        input_tokens=_count(raw.get("input_tokens")),
        output_tokens=_count(raw.get("output_tokens")),
        cache_write_tokens=cw_total,
        cache_read_tokens=_count(raw.get("cache_read_input_tokens")),
        cache_write_1h_tokens=cw_1h,
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
_NOISE_PREFIXES = (
    "<command-",
    "<local-command-",
    "<system-reminder>",
    "<user-prompt-submit-hook>",
    "<environment_context>",
    "<user_instructions>",
    "<ide_",
    "<task-notification",
    "[Request interrupted by user",
)


def _is_noise_prompt(text: str) -> bool:
    s = text.lstrip()
    return any(s.startswith(p) for p in _NOISE_PREFIXES)


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _is_event(ev: Dict[str, Any]) -> bool:
    return _str(ev.get("type")) in _EVENT_TYPES or isinstance(ev.get("sessionId"), str)


def _fs_str(path: str | os.PathLike) -> str:
    s = str(path)
    if os.name == "nt" and len(s) >= 240 and not s.startswith("\\\\"):
        return "\\\\?\\" + os.path.abspath(s)
    return s


def _subagent_roots(parent_path: Path, session_id: str) -> List[Tuple[Path, bool]]:
    """`[(folder, is_legacy)]`: every folder a sub-agent transcript of this parent could live
    in, whether or not it exists - `<project>/<session>/subagents/**` (nested `workflows`
    included) and the legacy `<project>/subagents/**`."""
    parent_dir = parent_path.parent
    names = [parent_path.stem]
    if (session_id and session_id != parent_path.stem
            and "/" not in session_id and "\\" not in session_id
            and session_id not in (".", "..")):
        names.append(session_id)
    roots = [(Path(_fs_str(parent_dir / name / "subagents")), False) for name in names]
    roots.append((Path(_fs_str(parent_dir / "subagents")), True))
    return roots


def _walk_subagents(root: Path) -> Tuple[List[Path], List[Path], bool]:
    """`(folders, transcripts, complete)`: the folders below `root`, breadth first, and every
    `*.jsonl` transcript in the tree - a transcript added anywhere in the tree changes the
    modification time of the folder that holds it, so watching the folders watches the whole
    tree, and the walk that decides what is watched also decides what is read.

    The folders are listed here, one by one, instead of through `pathlib.rglob`, which hides a
    listing error inside its own scan: an unreadable folder would look like an empty one, its
    transcripts would be missing from the totals, and the parse would still claim to have seen
    everything (LOCAL-R4-STATS-01). A transcript is a `*.jsonl` file, compared the way the
    volume compares names (so `*.JSONL` counts on Windows, as it did through `glob`).

    `complete` is False when the walk cannot promise it saw every folder and transcript: a tree
    holding more folders than `_MAX_SUBAGENT_FOLDERS` is watched at its shallowest ones only (a
    transcript written into a folder it never reached would change no watched source), and a
    folder that cannot be listed hides whatever it holds. A parse with an incomplete walk is not
    cached (LOCAL-R3-STATS-01)."""
    folders = [root]
    transcripts: List[Path] = []
    index = 0
    while index < len(folders):
        folder = folders[index]
        index += 1
        try:
            with os.scandir(folder) as listing:
                entries = sorted(listing, key=lambda entry: entry.name)
        except OSError:
            return folders, transcripts, False
        for entry in entries:
            if os.path.normcase(entry.name).endswith(".jsonl"):
                transcripts.append(Path(entry.path))
            try:
                if entry.is_dir() and not entry.is_symlink():
                    folders.append(Path(entry.path))
            except OSError:
                return folders, transcripts, False
    if len(folders) >= _MAX_SUBAGENT_FOLDERS:
        return folders[:_MAX_SUBAGENT_FOLDERS], transcripts, False
    return folders, transcripts, True


def _discover_subagent_files(parent_path: Path, session_id: str
                             ) -> Tuple[List[Tuple[Path, str]], List[Tuple[Path, bool]], bool]:
    """`(candidates, files, complete)`: how every folder a sub-agent transcript could live in
    turned out (a folder that is not there is a `SOURCE_MISSING` candidate, so a transcript
    written into it later is seen), the transcripts that are there now, and whether the whole
    tree was really read and watched. `complete` is False as soon as one folder could not be
    listed - by the watch or by the scan for the transcripts themselves - or the folder watch
    was capped, which is what keeps `runledger stats` from caching the parse."""
    if any(part.lower() == "subagents" for part in parent_path.parent.parts):
        return [], [], True
    candidates: List[Tuple[Path, str]] = []
    found: List[Tuple[Path, bool]] = []
    seen = set()
    complete = True
    for root, is_legacy in _subagent_roots(parent_path, session_id):
        try:
            is_dir = root.is_dir()
        except OSError:
            is_dir = False
            complete = False
        if not is_dir:
            candidates.append((root, SOURCE_MISSING))
            continue
        folders, entries, walked = _walk_subagents(root)
        candidates.extend((folder, SOURCE_OK) for folder in folders)
        complete = complete and walked
        for path in entries:
            key = str(path)
            if key not in seen:
                seen.add(key)
                found.append((path, is_legacy))

    return candidates, found, complete


def _merge_subagents(
    parent_path: Path,
    session_id: str,
    msg_usage: Dict[str, Usage],
    msg_model: Dict[str, Optional[str]],
) -> Tuple[List[Tuple[str, str]], int, bool]:
    """Merge sub-agent assistant message usage into `msg_usage`/`msg_model`.

    Returns `(sources, merged, complete)`: every path the merge read or looked for beside the
    parent transcript with how it turned out, how many transcripts contributed, and whether the
    tree really was watched in full. A transcript is a source whether or not it contributed, and
    a folder a sub-agent transcript could live in is one whether or not it exists, so
    `runledger stats` drops its cache record when one appears, is filled in or changes; an
    incomplete walk leaves nothing to watch at all, so `stats` caches no record for it."""
    candidates, files, complete = _discover_subagent_files(parent_path, session_id)
    sources: List[Tuple[str, str]] = [(str(path), state) for path, state in candidates]
    merged = 0
    valid_session_ids = {session_id, parent_path.stem}

    for sub_path, is_legacy in files:
        sub_events: List[Dict[str, Any]] = []
        sub_sids = set()
        try:
            with open(_fs_str(sub_path), "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except (ValueError, RecursionError):
                        continue
                    if not isinstance(ev, dict):
                        continue
                    if _is_event(ev):
                        sub_events.append(ev)
                        sid = _str(ev.get("sessionId")) or _str(ev.get("parentSessionId"))
                        if sid:
                            sub_sids.add(sid)
        except OSError:
            sources.append((str(sub_path), SOURCE_REJECTED))
            continue
        sources.append((str(sub_path), SOURCE_OK))

        if not sub_events:
            continue
        if is_legacy and sub_sids and not (sub_sids & valid_session_ids):
            continue

        merged += 1
        for idx, ev in enumerate(sub_events):
            if ev.get("type") != "assistant":
                continue
            msg = ev.get("message")
            msg = msg if isinstance(msg, dict) else {}
            mid = _str(msg.get("id")) or _str(ev.get("uuid")) or f"{sub_path}:{idx}"
            model = _str(msg.get("model")) or None
            if model == "<synthetic>":
                model = None
            u = _usage(msg.get("usage"))
            if mid not in msg_usage:
                msg_usage[mid] = u
                msg_model[mid] = model
            else:
                if u.total > msg_usage[mid].total:
                    msg_usage[mid] = u
                if not msg_model.get(mid) and model:
                    msg_model[mid] = model

    return sources, merged, complete



def parse_session(path: str | os.PathLike) -> Run:
    path = str(path)
    lines = 0
    valid_events = 0
    session_id = Path(path).stem
    cwd = git_branch = started = ended = None
    prompts: List[str] = []
    steps: List[Step] = []
    by_tool_id: Dict[str, Step] = {}
    seen_msg_ids: Dict[str, List[Step]] = {}
    msg_usage: Dict[str, Usage] = {}
    msg_model: Dict[str, Optional[str]] = {}
    final_message = ""

    with open(_fs_str(path), "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            lines += 1
            try:
                ev = json.loads(line)
            except (ValueError, RecursionError):
                continue  # tolerate partial lines from a live session, and absurdly deep nesting
            if not isinstance(ev, dict):
                continue
            if _is_event(ev):
                valid_events += 1

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
                    if not ev.get("isMeta") and content.strip() and not _is_noise_prompt(content):
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
                        if t and not _is_noise_prompt(t):
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

    if lines and not valid_events:
        raise ValueError(f"{path}: not a supported session file")

    subagent_sources, merged, watched = _merge_subagents(Path(path), session_id, msg_usage,
                                                         msg_model)
    notes: List[str] = []
    if merged:
        suffix = "s" if merged != 1 else ""
        notes.append(f"includes {merged} sub-agent transcript{suffix}")

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
        final_message=final_message, usage=total, models=models, notes=notes,
        sources=subagent_sources, dependencies_complete=watched,
    )


def peek_session(path: str | os.PathLike) -> Tuple[str, str, int, str, Optional[str]]:
    """Fast single-pass summary `(session_id, agent, steps_count, first_prompt, cwd)` for `list`.

    Skips `json.loads` on large `tool_result` or text-only lines once the header
    and first prompt have been seen, while counting every `tool_use` block.
    """
    p_str = str(path)
    lines = 0
    valid_events = 0
    session_id = Path(p_str).stem
    cwd: Optional[str] = None
    first_prompt: Optional[str] = None
    steps_count = 0

    with open(_fs_str(p_str), "r", encoding="utf-8", errors="replace") as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line:
                continue
            lines += 1
            if valid_events > 0 and 'tool_use"' not in line:
                if first_prompt is not None and (cwd is not None or '"cwd"' not in line[:512]):
                    continue
                head = line[:512]
                if 'tool_result"' in head and '"text"' not in head and '"cwd"' not in head:
                    continue
            try:
                ev = json.loads(line)
            except (ValueError, RecursionError):
                continue
            if not isinstance(ev, dict):
                continue
            if _is_event(ev):
                valid_events += 1
            etype = ev.get("type")
            if cwd is None:
                cwd = _str(ev.get("cwd")) or None
            sid = _str(ev.get("sessionId"))
            if sid:
                session_id = sid
            msg = ev.get("message")
            msg = msg if isinstance(msg, dict) else {}

            if etype == "user" and first_prompt is None:
                content = msg.get("content")
                if isinstance(content, str):
                    if not ev.get("isMeta") and content.strip() and not _is_noise_prompt(content):
                        first_prompt = content.strip()
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text" and not ev.get("isMeta"):
                            t = _str(block.get("text")).strip()
                            if t and not _is_noise_prompt(t):
                                first_prompt = t
                                break
            elif etype == "assistant" and 'tool_use"' in line:
                content = msg.get("content")
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_use":
                            steps_count += 1

    if lines and not valid_events:
        raise ValueError(f"{p_str}: not a supported session file")
    return session_id, "claude-code", steps_count, first_prompt or "", cwd


def claude_projects_dir() -> Path:
    """Claude Code's project folder: `$CLAUDE_CONFIG_DIR/projects`, else `<home>/.claude/projects`.

    Discovery, `guard install --global` and the doctor resolve the config directory through the
    one resolver in `hook_targets`, so a HOME that differs from USERPROFILE cannot split them
    (docs/agents.md)."""
    from .hook_targets import claude_dir
    return claude_dir() / "projects"


_WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:[\\/]")
_MAX_ENCODED_LEN = 200
_BASE36_DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"


def _java_hash_base36(text: str) -> str:
    """Java-style 32-bit signed hash over UTF-16 code units of `text`, formatted as base36(abs(h))."""
    raw = text.encode("utf-16-le")
    h = 0
    for i in range(0, len(raw), 2):
        unit = raw[i] | (raw[i + 1] << 8)
        h = (h * 31 + unit) & 0xFFFFFFFF
    if h >= 0x80000000:
        h -= 0x100000000
    n = abs(h)
    if n == 0:
        return "0"
    out = []
    while n:
        n, r = divmod(n, 36)
        out.append(_BASE36_DIGITS[r])
    return "".join(reversed(out))


def encode_project_path(project: str) -> str:
    """Claude Code names project folders by replacing every non-alphanumeric
    character of the project path with '-': '/home/dev/my.app' becomes
    '-home-dev-my-app' and 'D:\\runledger' becomes 'D--runledger'.

    When the sanitized name exceeds 200 characters, Claude Code truncates to
    200 characters and appends '-' + base36(abs(java_hash_utf16(project))).

    Paths that are already absolute (POSIX or Windows form) are encoded as
    given; only relative paths are resolved against the current directory."""
    if not (project.startswith("/") or _WINDOWS_ABSOLUTE.match(project)):
        project = os.path.abspath(project)
    sanitized = "".join(ch if ch.isascii() and ch.isalnum() else "-" for ch in project)
    if len(sanitized) <= _MAX_ENCODED_LEN:
        return sanitized
    return f"{sanitized[:_MAX_ENCODED_LEN]}-{_java_hash_base36(project)}"


def mtime(path: Path) -> float:
    """Modified time, or 0 for a file that vanished after it was listed (live sessions rotate)."""
    try:
        return os.stat(_fs_str(path)).st_mtime
    except OSError:
        return 0.0


def _list_jsonl_in_dir(d: Path) -> List[Path]:
    fs_d = Path(_fs_str(d))
    try:
        if not fs_d.is_dir():
            return []
        return [d / f.name for f in fs_d.glob("*.jsonl")]
    except OSError:
        return []


def find_sessions(project: Optional[str] = None) -> List[Path]:
    root = claude_projects_dir()
    fs_root = Path(_fs_str(root))
    try:
        if not fs_root.exists():
            return []
    except OSError:
        return []
    if project:
        encoded = encode_project_path(project)
        files = _list_jsonl_in_dir(root / encoded)
        if not files and len(encoded) > _MAX_ENCODED_LEN:
            prefix = encoded[:_MAX_ENCODED_LEN] + "-"
            prefix_low = prefix.lower()
            try:
                children = sorted(fs_root.iterdir(), key=lambda p: p.name)
            except OSError:
                children = []
            for cand in children:
                name = cand.name
                if name.startswith(prefix) or (os.name == "nt" and name.lower().startswith(prefix_low)):
                    files.extend(_list_jsonl_in_dir(root / name))
    else:
        try:
            files = [
                root / f.parent.name / f.name
                for f in fs_root.glob("*/*.jsonl")
                if f.parent.name.lower() != "subagents"
            ]
        except OSError:
            files = []
    files = [
        f for f in files
        if not f.name.startswith("agent-")
        and not any(part.lower() == "subagents" for part in f.parts)
    ]
    return sorted(files, key=mtime, reverse=True)

