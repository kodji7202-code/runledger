"""Aider adapter: reads the chat log Aider appends to the project root.

The history file is `.aider.chat.history.md`. Each session starts with a
`# aider chat started at YYYY-MM-DD HH:MM:SS` line. Inside a session:

  #### text        user prompt (consecutive `####` lines form one prompt)
  > text           aider's own output: model line, `Added X to the chat`,
                   `Applied edit to X`, `Commit <sha> <msg>`,
                   `Tokens: ... Cost: ...`, `Running <cmd>`, prompts
  anything else    assistant reply (markdown). SEARCH/REPLACE blocks are edits.
                   ```bash blocks are only suggestions; they become shell steps
                   only when aider echoes them as `> Running <cmd>`.

Mapping to the canonical vocabulary (runledger/adapters/__init__.py):
  SEARCH/REPLACE          -> Edit {file_path, old_string, new_string}
  empty SEARCH            -> Write {file_path, content}
  > Added X to the chat   -> Read {file_path}
  > Running X             -> Bash {command}
  #### /run X, /test X     -> Bash {command}  (the echoed `> Running X` is not counted twice)
  > Commit <sha> <msg>    -> GitCommit {sha, message}  (not canonical, so the name is kept)

One Run per session. parse(path) returns the last session; parse_all(path)
returns every session in file order. Aider reports tokens per reply, so usage
is split across the Edit/Write steps of that reply only.
"""
from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..parser import Run, Step, Usage

NAME = "aider"
LABEL = "Aider"
HISTORY_FILE = ".aider.chat.history.md"

_HEADER_RE = re.compile(r"^# aider chat started at (.+?)\s*$")
_SEARCH_RE = re.compile(r"^<{5,9} SEARCH\s*$")
_SEP_RE = re.compile(r"^={5,9}\s*$")
_REPLACE_RE = re.compile(r"^>{5,9} REPLACE\s*$")
_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")
_COMMAND_RE = re.compile(r"^/([A-Za-z][\w-]*)(?:\s+(.*))?$")
_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")
_MODEL_RE = re.compile(r"^Main model:\s*(\S+)")
_ADDED_RE = re.compile(r"^Added (.+?) to the chat\b")
_COUNT_NOUN_RE = re.compile(r"^\d+ files?$")
_APPLIED_RE = re.compile(r"^Applied edit to (.+?)\s*$")
_COMMIT_RE = re.compile(r"^Commit ([0-9a-fA-F]{4,40})(?:\s+(.*?))?\s*$")
_RUNNING_RE = re.compile(r"^Running (.+?)\s*$")
_TOKENS_RE = re.compile(r"Tokens:\s*([\d.,]+[kKmM]?)\s+sent\b.*?([\d.,]+[kKmM]?)\s+received\b")
_COST_RE = re.compile(r"\$\s*([\d.,]+)\s+session\b")
_COUNT_RE = re.compile(r"^([\d.,]+)([kKmM]?)$")

# Aider housekeeping commands: they change aider's state, not what the user asked for.
# /run, /test, /code, /ask, /architect and unknown slash text stay as prompts.
_DROP_COMMANDS = frozenset({
    "add", "drop", "clear", "exit", "quit", "help", "tokens", "ls", "undo", "reset",
    "settings", "model", "diff", "commit", "git", "version", "report", "map",
    "map-refresh", "read-only", "copy", "copy-context", "paste", "editor", "history",
    "load", "save", "multiline-mode", "think-tokens", "reasoning-effort", "show-prompts",
    "clipboard", "cache-prompts",
})


def detect(path) -> bool:
    return Path(path).name.lower().endswith(HISTORY_FILE)


def parse(path) -> Run:
    """The last session in the file (an empty Run if the file has none)."""
    p = Path(path)
    runs = parse_all(p)
    return runs[-1] if runs else _build_run(p, None, [])


def parse_all(path) -> List[Run]:
    p = Path(path)
    return [_build_run(p, header, lines) for header, lines in _split_sessions(_read(p))]


def find_sessions(project: Optional[str] = None) -> List[Path]:
    # Aider keeps no global index, so only the project's own history file counts.
    if not project:
        return []
    f = Path(project) / HISTORY_FILE
    return [f] if f.is_file() else []


# --- file level --------------------------------------------------------------

def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig", errors="replace")


def _split_sessions(text: str) -> List[Tuple[Optional[str], List[str]]]:
    """(raw header timestamp, body lines) per session. Text before the first
    header is ignored. A file with no header at all is one headerless session."""
    lines = text.splitlines()
    starts = []
    for i, line in enumerate(lines):
        m = _HEADER_RE.match(line)
        if m:
            starts.append((i, m.group(1)))
    if not starts:
        return [(None, lines)] if any(line.strip() for line in lines) else []
    out = []
    for k, (i, header) in enumerate(starts):
        end = starts[k + 1][0] if k + 1 < len(starts) else len(lines)
        out.append((header, lines[i + 1:end]))
    return out


def _build_run(path: Path, header: Optional[str], lines: List[str]) -> Run:
    session = _Session(lines)
    session.scan()
    events = session.events
    applied = [(pos, ev) for pos, ev in enumerate(events) if ev["kind"] == "applied"]

    steps: List[Step] = []
    pending: List[Step] = []   # Edit/Write steps waiting for the reply's token line
    total = Usage()
    models: Dict[str, Usage] = {}
    for pos, ev in enumerate(events):
        kind = ev["kind"]
        if kind == "turn":
            pending = []
        elif kind == "step":
            steps.append(_step(len(steps) + 1, ev["tool"], ev["input"], ev["model"]))
        elif kind == "edit":
            # When a session reports applied edits, keep only blocks confirmed by a
            # later "Applied edit to <file>" in the same prompt turn.
            if applied and not any(
                p > pos and a["turn"] == ev["turn"] and _same_file(a["file"], ev["file"])
                for p, a in applied
            ):
                continue
            if ev["old"]:
                step = _step(len(steps) + 1, "Edit", {
                    "file_path": ev["file"], "old_string": ev["old"], "new_string": ev["new"],
                }, ev["model"])
            else:
                step = _step(len(steps) + 1, "Write", {
                    "file_path": ev["file"], "content": ev["new"],
                }, ev["model"])
            steps.append(step)
            pending.append(step)
        elif kind == "tokens":
            usage = ev["usage"]
            total.add(usage)
            models.setdefault(ev["model"] or "unknown", Usage()).add(usage)
            if pending:
                n = len(pending)
                for st in pending:
                    st.usage = Usage(usage.input_tokens // n, usage.output_tokens // n)
                pending = []

    stamp = session.timestamps[-1] if session.timestamps else None
    started = _iso(header)
    return Run(
        session_id="aider-" + hashlib.sha1((str(path) + (header or "")).encode("utf-8")).hexdigest()[:12],
        path=str(path),
        cwd=os.path.dirname(os.path.abspath(str(path))),
        git_branch=None,
        started=started,
        ended=_iso(stamp) or started,
        prompts=session.prompts,
        steps=steps,
        final_message=_final_message(lines, session.consumed),
        usage=total,
        models=models,
        agent=NAME,
        reported_cost=session.reported_cost,
    )


# --- one session -------------------------------------------------------------

class _Session:
    """Scans one session's body into ordered events. Lines that are not prose
    (prompts, aider output, code, edit blocks) are recorded in `consumed`."""

    def __init__(self, lines: List[str]) -> None:
        self.lines = lines
        self.events: List[Dict[str, Any]] = []
        self.consumed: set = set()
        self.prompts: List[str] = []
        self.timestamps: List[str] = []
        self.reported_cost: Optional[float] = None
        self.model: Optional[str] = None
        self.turn = 0
        self.group: List[str] = []       # lines of the user prompt being read
        self.cmds: List[str] = []        # /run and /test commands in this turn
        self.fence: Optional[str] = None  # opening fence while inside a code block

    def scan(self) -> None:
        lines = self.lines
        i = 0
        while i < len(lines):
            line = lines[i]
            if _SEARCH_RE.match(line):
                self._flush()
                i = self._edit_block(i)
                continue
            if line.startswith("> ") or line == ">":
                self._flush()
                self.fence = None
                self.consumed.add(i)
                self._info(line[2:])
                i += 1
                continue
            if self.fence is not None:
                self.consumed.add(i)
                if _closes(line, self.fence):
                    self.fence = None
                i += 1
                continue
            if line.startswith("#### ") or line.rstrip() == "####":
                self.group.append(line[5:])
                self.consumed.add(i)
                i += 1
                continue
            self._flush()
            if line.strip():
                fence = _FENCE_RE.match(line)
                if fence:
                    self.fence = fence.group(1)
                    self.consumed.add(i)
                elif _REPLACE_RE.match(line) or _SEP_RE.match(line):
                    self.consumed.add(i)   # stray block marker outside any block
            i += 1
        self._flush()

    def _flush(self) -> None:
        """Close the pending user prompt: it starts a new turn."""
        if not self.group:
            return
        lines, self.group = self.group, []
        self.turn += 1
        self.cmds = []
        self.events.append({"kind": "turn"})
        first = _COMMAND_RE.match(lines[0].strip())
        if first:
            name = first.group(1).lower()
            if name in _DROP_COMMANDS:
                return
            if name in ("run", "test"):
                cmd = (first.group(2) or "").strip()
                if cmd:
                    self.cmds.append(cmd)
                    self._add_step("Bash", {"command": cmd})
        text = "\n".join(line.rstrip() for line in lines).strip()
        if text:
            self.prompts.append(text)

    def _info(self, text: str) -> None:
        stamp = _TIMESTAMP_RE.search(text)
        if stamp:
            self.timestamps.append(stamp.group(0))
        model = _MODEL_RE.match(text)
        if model:
            self.model = _strip_provider(model.group(1))
            return
        if "Tokens:" in text:
            self._tokens(text)
            return
        added = _ADDED_RE.match(text)
        if added and not _COUNT_NOUN_RE.match(added.group(1)):
            self._add_step("Read", {"file_path": added.group(1)})
            return
        applied = _APPLIED_RE.match(text)
        if applied:
            self.events.append({"kind": "applied", "turn": self.turn, "file": applied.group(1)})
            return
        commit = _COMMIT_RE.match(text)
        if commit:
            self._add_step("GitCommit", {"sha": commit.group(1), "message": commit.group(2) or ""})
            return
        running = _RUNNING_RE.match(text)
        if running:
            cmd = running.group(1)
            if cmd in self.cmds:   # already recorded from the /run or /test prompt
                self.cmds.remove(cmd)
                return
            self._add_step("Bash", {"command": cmd})

    def _tokens(self, text: str) -> None:
        m = _TOKENS_RE.search(text)
        if m:
            sent, received = _count(m.group(1)), _count(m.group(2))
            if sent is not None and received is not None:
                self.events.append({
                    "kind": "tokens",
                    "usage": Usage(input_tokens=sent, output_tokens=received),
                    "model": self.model,
                })
        cost = _COST_RE.search(text)
        if cost:
            value = _number(cost.group(1))
            if value is not None:
                self.reported_cost = value   # aider's running session total; last one wins

    def _add_step(self, tool: str, inp: Dict[str, Any]) -> None:
        self.events.append({"kind": "step", "tool": tool, "input": inp, "model": self.model})

    def _edit_block(self, start: int) -> int:
        """Parse a SEARCH/REPLACE block whose SEARCH line is at `start`.
        Returns the index to continue scanning from."""
        lines = self.lines
        n = len(lines)
        sep = start + 1
        while sep < n and not _SEP_RE.match(lines[sep]):
            if _SEARCH_RE.match(lines[sep]) or _REPLACE_RE.match(lines[sep]):
                break
            sep += 1
        if sep >= n or not _SEP_RE.match(lines[sep]):
            self.consumed.add(start)   # malformed: no '=======' line; drop the marker only
            return start + 1
        end = sep + 1
        while end < n and not _REPLACE_RE.match(lines[end]):
            if _SEARCH_RE.match(lines[end]) or _SEP_RE.match(lines[end]):
                break
            end += 1
        if end >= n or not _REPLACE_RE.match(lines[end]):
            self.consumed.add(start)   # malformed or truncated: no REPLACE terminator
            return start + 1
        for k in range(start, end + 1):
            self.consumed.add(k)
        found = self._filename_before(start)
        if found is None:
            return end + 1             # no usable file name: block is dropped
        fidx, name = found
        self.consumed.add(fidx)
        self.events.append({
            "kind": "edit",
            "turn": self.turn,
            "file": name,
            "old": _join(lines[start + 1:sep]),
            "new": _join(lines[sep + 1:end]),
            "model": self.model,
        })
        return end + 1

    def _filename_before(self, start: int) -> Optional[Tuple[int, str]]:
        """The file name is the nearest line above the block, skipping blank
        lines and the opening code fence."""
        j = start - 1
        while j >= 0 and (not self.lines[j].strip() or _FENCE_RE.match(self.lines[j])):
            j -= 1
        if j < 0:
            return None
        name = _clean_filename(self.lines[j])
        return (j, name) if name else None


# --- helpers -----------------------------------------------------------------

def _step(index: int, tool: str, inp: Dict[str, Any], model: Optional[str]) -> Step:
    return Step(index=index, tool=tool, input=inp, tool_use_id=f"aider-{index}",
                model=model, timestamp=None)


def _join(lines: List[str]) -> str:
    return "".join(line + "\n" for line in lines)


def _closes(line: str, opener: str) -> bool:
    s = line.strip()
    return bool(s) and set(s) == {opener[0]} and len(s) >= len(opener)


def _clean_filename(line: str) -> Optional[str]:
    s = re.sub(r"^[-*+]\s+", "", line.strip()).strip("`*\"' \t")
    if not s or len(s) > 300 or s[0] in "><#=|" or s[-1] in ":.":
        return None
    if re.search(r"\s", s) and not re.search(r"[\\/]", s):
        return None   # prose such as "Here is the change" is not a path
    return s


def _norm_path(p: str) -> str:
    s = p.strip().strip("`*\"'").replace("\\", "/")
    while s.startswith("./"):
        s = s[2:]
    return s.lower()


def _same_file(a: str, b: str) -> bool:
    x, y = _norm_path(a), _norm_path(b)
    return x == y or x.endswith("/" + y) or y.endswith("/" + x)


def _strip_provider(model: str) -> str:
    """'anthropic/claude-sonnet-4-5' and 'openrouter/anthropic/claude-sonnet-4-5'
    both become 'claude-sonnet-4-5'."""
    return model.rsplit("/", 1)[-1]


def _number(text: str) -> Optional[float]:
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None


def _count(text: str) -> Optional[int]:
    """'850' -> 850, '2.5k' -> 2500, '1.2M' -> 1200000, '1,234' -> 1234."""
    m = _COUNT_RE.match(text.strip())
    if not m:
        return None
    value = _number(m.group(1))
    if value is None:
        return None
    scale = {"k": 1_000, "m": 1_000_000}.get(m.group(2).lower(), 1)
    return int(round(value * scale))


def _iso(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    try:
        return datetime.strptime(raw.strip(), "%Y-%m-%d %H:%M:%S").isoformat()
    except ValueError:
        return None


def _final_message(lines: List[str], consumed: set) -> str:
    """The last paragraph of assistant prose: runs of prose lines separated by
    blank lines, aider output, prompts, code and edit blocks."""
    best = ""
    buf: List[str] = []
    for idx, line in enumerate(lines):
        if idx in consumed or not line.strip():
            if buf:
                best = "\n".join(buf).strip()
                buf = []
            continue
        buf.append(line.rstrip())
    if buf:
        best = "\n".join(buf).strip()
    return best
