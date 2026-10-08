"""OpenAI Codex CLI adapter.

Codex writes one rollout file per session:
  $CODEX_HOME/sessions/YYYY/MM/DD/rollout-<timestamp>-<uuid>.jsonl
CODEX_HOME defaults to ~/.codex.

Current rollouts wrap every line as {"timestamp", "type", "payload"}. Older
rollouts have no envelope: items sit at top level ({"type": "message", ...})
and the first line is a header ({"id", "timestamp", "instructions"}). Both forms
are read. Lines that are not JSON objects, or have an unknown shape, are skipped.

Token accounting: each token_count event carries last_token_usage (the model
call that just finished) and total_token_usage (cumulative for the session).
The run total is the last cumulative figure. Each call's usage is split evenly
across the steps issued since the previous token_count event. Calls that issued
no steps count toward the run only, as in the Claude Code adapter.
"""
from __future__ import annotations

import json
import os
import posixpath
import re
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from ..parser import Run, Step, Usage

NAME = "codex"
LABEL = "Codex CLI"

RESULT_LIMIT = 2000
CONTEXT_PREFIXES = ("<environment_context>", "<user_instructions>")
SHELL_FUNCTIONS = {"shell", "container.exec", "shell_command", "exec_command"}
SHELL_PROGRAMS = {"bash", "sh", "zsh", "powershell", "pwsh"}
SCRIPT_FLAGS = {"-lc", "-c", "-command"}
LEGACY_ITEM_TYPES = {"message", "reasoning", "function_call", "function_call_output",
                     "local_shell_call", "custom_tool_call", "custom_tool_call_output"}
USAGE_FIELDS = ("input_tokens", "output_tokens", "cache_write_tokens", "cache_read_tokens")

_DRIVE = re.compile(r"^[A-Za-z]:")
_PATCH_HEADER = re.compile(r"^\*\*\* (Add File|Update File|Delete File|Move to):\s*(.+?)\s*$")
_PATCH_KIND = {"Add File": "add", "Update File": "update", "Delete File": "delete"}


# ---------------------------------------------------------------- locating files

def codex_home() -> Path:
    base = os.environ.get("CODEX_HOME")
    return Path(base).expanduser() if base else Path.home() / ".codex"


def detect(path: Path) -> bool:
    p = Path(path)
    if p.suffix != ".jsonl":
        return False
    if p.name.startswith("rollout-") and any(part.lower() == "sessions" for part in p.parent.parts):
        return True
    first = _first_line_object(p)
    if first is None:
        return False
    if "type" in first:
        return first.get("type") == "session_meta"
    return "instructions" in first


def find_sessions(project: Optional[str] = None) -> List[Path]:
    root = codex_home() / "sessions"
    if not root.is_dir():
        return []
    wanted = _norm_path(_absolute(str(project))) if project else None
    found = []
    for f in root.rglob("rollout-*.jsonl"):
        if not f.is_file():
            continue
        if wanted is not None and _norm_path(_session_cwd(f) or "") != wanted:
            continue
        found.append(f)
    return sorted(found, key=_mtime, reverse=True)


def _first_line_object(path: Path) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (ValueError, RecursionError):
                    return None
                return obj if isinstance(obj, dict) else None
    except OSError:
        return None
    return None


def _session_cwd(path: Path) -> Optional[str]:
    """cwd from the session header, or from the first turn context, within the first lines."""
    turn_cwd = None
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
            for n, line in enumerate(fh):
                if n >= 50:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (ValueError, RecursionError):
                    continue
                env = _envelope(obj) if isinstance(obj, dict) else None
                if env is None:
                    continue
                kind, payload, _ = env
                if kind == "session_meta" and _str(payload.get("cwd")):
                    return payload["cwd"]
                if kind == "turn_context" and turn_cwd is None:
                    turn_cwd = _str(payload.get("cwd"))
    except OSError:
        return None
    return turn_cwd


def _absolute(project: str) -> str:
    if project.startswith(("/", "\\")) or _DRIVE.match(project):
        return project
    return os.path.abspath(project)


def _norm_path(path: str) -> str:
    s = path.strip().replace("\\", "/")
    if s:
        s = posixpath.normpath(s)
    if sys.platform == "win32" or _DRIVE.match(s):
        s = s.lower()  # Windows paths compare case-insensitively
    return s


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


# ---------------------------------------------------------------- parsing

def parse(path: Path) -> Run:
    rollout = _Rollout(str(path))
    for obj in _read_lines(path):
        try:
            rollout.feed(obj)
        except (AttributeError, IndexError, KeyError, TypeError, ValueError):
            continue  # a line with an unexpected shape must not stop the parse
    return rollout.finish()


def _read_lines(path: Path) -> Iterator[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:  # sig: tolerate a BOM
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except (ValueError, RecursionError):
                continue  # tolerate partial lines from a live session
            if isinstance(obj, dict):
                yield obj


def _envelope(obj: Dict[str, Any]) -> Optional[Tuple[str, Dict[str, Any], Optional[str]]]:
    """(kind, payload, timestamp) for one rollout line, or None when it is not understood."""
    ts = _str(obj.get("timestamp"))
    kind = obj.get("type")
    if isinstance(kind, str) and isinstance(obj.get("payload"), dict):
        return kind, obj["payload"], ts
    if isinstance(kind, str):
        # old format: the item itself sits at top level
        return ("response_item", obj, ts) if kind in LEGACY_ITEM_TYPES else None
    if "instructions" in obj or "id" in obj:
        return "session_meta", obj, ts  # old format: header line without a type
    return None


class _Rollout:
    def __init__(self, path: str):
        self.path = path
        self.meta_seen = False
        self.session_id: Optional[str] = None
        self.meta_cwd: Optional[str] = None
        self.meta_model: Optional[str] = None
        self.git_branch: Optional[str] = None
        self.turn_cwd: Optional[str] = None
        self.model: Optional[str] = None
        self.started: Optional[str] = None
        self.ended: Optional[str] = None
        self.prompts_event: List[str] = []
        self.prompts_item: List[str] = []
        self.final_message = ""
        self.steps: List[Step] = []
        self.pending: List[Step] = []          # steps waiting for the next token_count
        self.by_call: Dict[str, List[Step]] = {}
        self.outputs: Dict[str, Tuple[str, Optional[int]]] = {}
        self.event_results: Dict[str, Tuple[str, bool]] = {}
        self.total: Optional[Usage] = None
        self.attributed = Usage()
        self.models: Dict[str, Usage] = {}

    def model_name(self) -> str:
        return self.model or self.meta_model or "unknown"

    def feed(self, obj: Dict[str, Any]) -> None:
        env = _envelope(obj)
        if env is None:
            return
        kind, payload, ts = env
        if ts:
            self.started = self.started or ts
            self.ended = ts
        if kind == "session_meta":
            self._session_meta(payload)
        elif kind == "turn_context":
            self._turn_context(payload)
        elif kind == "event_msg":
            self._event(payload)
        elif kind == "response_item":
            self._item(payload, ts)

    def finish(self) -> Run:
        total = self.total if self.total is not None else _minus(self.attributed, Usage())
        tail = _minus(total, self.attributed)  # usage not covered by any token_count event
        if self.pending:
            for step, share in zip(self.pending, _split(tail, len(self.pending))):
                step.usage = share
        self.models.setdefault(self.model_name(), Usage()).add(tail)

        for call_id, steps in self.by_call.items():
            if call_id in self.outputs:
                text, code = self.outputs[call_id]
                is_error = _exit_failed(code)
            elif call_id in self.event_results:
                text, is_error = self.event_results[call_id]
            else:
                continue
            for step in steps:
                step.result_text = text[:RESULT_LIMIT]
                step.is_error = is_error

        return Run(
            session_id=self.session_id or Path(self.path).stem,
            path=self.path,
            cwd=self.meta_cwd or self.turn_cwd,
            git_branch=self.git_branch,
            started=self.started,
            ended=self.ended,
            prompts=self.prompts_event or self.prompts_item,
            steps=self.steps,
            final_message=self.final_message,
            usage=total,
            models=self.models,
            agent=NAME,
        )

    # -- session header and turn context

    def _session_meta(self, p: Dict[str, Any]) -> None:
        if self.meta_seen:
            return
        self.meta_seen = True
        self.session_id = _str(p.get("id"))
        self.meta_cwd = _str(p.get("cwd"))
        self.meta_model = _str(p.get("model"))
        git = p.get("git")
        if isinstance(git, dict):
            self.git_branch = _str(git.get("branch"))

    def _turn_context(self, p: Dict[str, Any]) -> None:
        if self.turn_cwd is None:
            self.turn_cwd = _str(p.get("cwd"))
        model = _str(p.get("model"))
        if model:
            self.model = model

    # -- event_msg: user and agent messages, token counts, command results

    def _event(self, p: Dict[str, Any]) -> None:
        etype = p.get("type")
        if etype == "user_message":
            text = _str(p.get("message"))
            if text and text.strip() and not _is_context(text):
                self.prompts_event.append(text.strip())
        elif etype == "agent_message":
            text = _str(p.get("message"))
            if text and text.strip():
                self.final_message = text.strip()
        elif etype == "token_count":
            self._token_count(p.get("info"))
        elif etype == "exec_command_end":
            text = (_str(p.get("aggregated_output")) or _str(p.get("formatted_output"))
                    or (_str(p.get("stdout")) or "") + (_str(p.get("stderr")) or ""))
            self._note_result(p.get("call_id"), text, _exit_failed(p.get("exit_code")))
        elif etype == "patch_apply_end":
            text = _str(p.get("stdout")) or _str(p.get("stderr")) or ""
            self._note_result(p.get("call_id"), text, p.get("success") is False)

    def _note_result(self, call_id: Any, text: str, is_error: bool) -> None:
        if call_id:
            self.event_results[str(call_id)] = (text, is_error)

    def _token_count(self, info: Any) -> None:
        if not isinstance(info, dict):
            return
        if isinstance(info.get("total_token_usage"), dict):
            self.total = _usage(info["total_token_usage"])
        if isinstance(info.get("last_token_usage"), dict):
            self._attribute(_usage(info["last_token_usage"]))

    def _attribute(self, u: Usage) -> None:
        self.attributed.add(u)
        self.models.setdefault(self.model_name(), Usage()).add(u)
        if self.pending:
            for step, share in zip(self.pending, _split(u, len(self.pending))):
                step.usage = share
            self.pending = []

    # -- response_item: messages and tool calls

    def _item(self, p: Dict[str, Any], ts: Optional[str]) -> None:
        ptype = p.get("type")
        if ptype == "message":
            blocks = _text_blocks(p.get("content"))
            if p.get("role") == "user":
                kept = "\n".join(b for b in blocks if b.strip() and not _is_context(b)).strip()
                if kept:
                    self.prompts_item.append(kept)
            elif p.get("role") == "assistant":
                text = "\n".join(blocks).strip()
                if text:
                    self.final_message = text
        elif ptype == "function_call":
            self._function_call(p, ts)
        elif ptype == "custom_tool_call":
            if p.get("name") == "apply_patch":
                self._patch(_str(p.get("input")) or "", p.get("call_id"), ts)
            else:
                name = str(p.get("name") or "?")
                self._add_step(_tool_name(name), {"input": p.get("input")}, p.get("call_id"), ts)
        elif ptype == "local_shell_call":
            action = p.get("action") if isinstance(p.get("action"), dict) else {}
            self._shell(action.get("command"), p.get("call_id") or p.get("id"), ts)
        elif ptype in ("function_call_output", "custom_tool_call_output"):
            call_id = p.get("call_id")
            if call_id:
                self.outputs[str(call_id)] = _output_of(p.get("output"))

    def _function_call(self, p: Dict[str, Any], ts: Optional[str]) -> None:
        name = str(p.get("name") or "?")
        call_id = p.get("call_id")
        args = _args(p.get("arguments"))
        if name == "apply_patch":
            self._patch(_str(args.get("input")) or _str(args.get("arguments")) or "", call_id, ts)
        elif name in SHELL_FUNCTIONS:
            self._shell(args.get("command", args.get("cmd", args.get("arguments"))), call_id, ts)
        else:
            self._add_step(_tool_name(name), args, call_id, ts)

    def _shell(self, value: Any, call_id: Any, ts: Optional[str]) -> None:
        patch = _patch_in_shell(value)
        if patch is not None:
            self._patch(patch, call_id, ts)
            return
        tool, command = _shell_call(value)
        self._add_step(tool, {"command": command}, call_id, ts)

    def _patch(self, text: str, call_id: Any, ts: Optional[str]) -> None:
        for tool, inp in patch_to_steps(text):
            self._add_step(tool, inp, call_id, ts)

    def _add_step(self, tool: str, inp: Dict[str, Any], call_id: Any, ts: Optional[str]) -> None:
        step = Step(index=len(self.steps) + 1, tool=tool, input=inp,
                    tool_use_id=str(call_id or ""), model=self.model_name(), timestamp=ts)
        self.steps.append(step)
        self.pending.append(step)
        if call_id:
            self.by_call.setdefault(str(call_id), []).append(step)


# ---------------------------------------------------------------- tool mapping

def _shell_call(value: Any) -> Tuple[str, str]:
    """(tool, command string) for a shell call's command value: an argv list or a string."""
    if isinstance(value, str):
        return "Bash", value
    if not isinstance(value, list) or not value:
        return "Bash", ""
    argv = [str(a) for a in value]
    tool = "PowerShell" if _program(argv[0]) in ("powershell", "pwsh") else "Bash"
    script = _wrapped_script(argv)
    return tool, script if script is not None else shlex.join(argv)


def _wrapped_script(argv: List[str]) -> Optional[str]:
    """SCRIPT from ["bash", "-lc", SCRIPT] or ["powershell", "-Command", SCRIPT]."""
    if _program(argv[0]) not in SHELL_PROGRAMS:
        return None
    for i in range(1, len(argv) - 1):
        if argv[i].lower() in SCRIPT_FLAGS:
            return argv[i + 1]
    return None


def _patch_in_shell(value: Any) -> Optional[str]:
    """The patch body when a shell call is really apply_patch, else None."""
    if isinstance(value, list) and value and _program(str(value[0])) == "apply_patch":
        return str(value[1]) if len(value) > 1 else ""
    _, command = _shell_call(value)
    start = command.find("*** Begin Patch")
    if start >= 0 and command.lstrip().startswith("apply_patch"):
        return command[start:]
    return None


def _tool_name(name: str) -> str:
    """Codex names MCP tools server__tool; the canonical form is mcp__server__tool."""
    if name.startswith("mcp__") or "__" not in name:
        return name
    return "mcp__" + name


def _program(arg: str) -> str:
    name = re.split(r"[\\/]", arg.strip())[-1].lower()
    return name[:-4] if name.endswith(".exe") else name


@dataclass
class _Op:
    kind: str                   # add | update | delete
    path: str
    move: Optional[str] = None  # update only: destination of "*** Move to:"
    lines: List[str] = field(default_factory=list)                          # add: file body
    hunks: List[Tuple[List[str], List[str]]] = field(default_factory=list)  # update: (old, new)


def _parse_ops(patch: str) -> List[_Op]:
    ops: List[_Op] = []
    cur: Optional[_Op] = None
    hunk: Optional[Tuple[List[str], List[str]]] = None
    for line in patch.splitlines():
        if line.startswith("*** Begin Patch") or line.startswith("*** End of File"):
            continue
        if line.startswith("*** End Patch"):
            cur = hunk = None
            continue
        m = _PATCH_HEADER.match(line)
        if m:
            kind, path = m.groups()
            hunk = None
            if kind == "Move to":
                if cur is not None and cur.kind == "update":
                    cur.move = path
                continue
            cur = _Op(_PATCH_KIND[kind], path)
            ops.append(cur)
            if cur.kind == "delete":
                cur = None  # no body follows
            continue
        if cur is None:
            continue
        if cur.kind == "add":
            if line.startswith("+"):
                cur.lines.append(line[1:])
            continue
        if line.startswith("@@") or hunk is None:
            hunk = ([], [])
            cur.hunks.append(hunk)
            if line.startswith("@@"):
                continue
        if line.startswith("+"):
            hunk[1].append(line[1:])
        elif line.startswith("-"):
            hunk[0].append(line[1:])
        elif line.startswith(" "):
            hunk[0].append(line[1:])
            hunk[1].append(line[1:])
        elif line == "":
            hunk[0].append("")
            hunk[1].append("")
    return ops


def patch_to_steps(patch: str) -> List[Tuple[str, Dict[str, Any]]]:
    """Canonical (tool, input) pairs for a Codex apply_patch body, one per file.

    Add -> Write; Delete -> Delete; Update -> Edit (one hunk) or MultiEdit (several
    hunks). A move emits the edit on the destination path, then a Delete of the source.
    """
    out: List[Tuple[str, Dict[str, Any]]] = []
    for op in _parse_ops(patch):
        if op.kind == "add":
            content = "".join(line + "\n" for line in op.lines)
            out.append(("Write", {"file_path": op.path, "content": content}))
        elif op.kind == "delete":
            out.append(("Delete", {"file_path": op.path}))
        else:
            target = op.move or op.path
            edits = [{"old_string": "\n".join(old), "new_string": "\n".join(new)}
                     for old, new in op.hunks if any(old) or any(new)]
            if len(edits) == 1:
                out.append(("Edit", {"file_path": target, **edits[0]}))
            elif edits:
                out.append(("MultiEdit", {"file_path": target, "edits": edits}))
            elif op.move:
                out.append(("Edit", {"file_path": target, "old_string": "", "new_string": ""}))
            if op.move:
                out.append(("Delete", {"file_path": op.path}))
    return out


# ---------------------------------------------------------------- values and usage

def _str(value: Any) -> Optional[str]:
    return value if isinstance(value, str) else None


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _exit_failed(code: Any) -> bool:
    return _is_int(code) and code != 0


def _is_context(text: str) -> bool:
    return text.lstrip().startswith(CONTEXT_PREFIXES)


def _text_blocks(content: Any) -> List[str]:
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    out = []
    for block in content:
        if isinstance(block, str):
            out.append(block)
        elif isinstance(block, dict) and isinstance(block.get("text"), str):
            out.append(block["text"])
    return out


def _args(raw: Any) -> Dict[str, Any]:
    """Arguments of a call: a JSON object string, or {"arguments": raw} when it is not one."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            value = json.loads(raw)
        except (ValueError, RecursionError):
            return {"arguments": raw}
        return value if isinstance(value, dict) else {"arguments": raw}
    return {}


def _output_of(raw: Any) -> Tuple[str, Optional[int]]:
    """(text, exit_code) from a call output: plain text, or a JSON string/object
    {"output": ..., "metadata": {"exit_code": ...}}."""
    obj = raw
    if isinstance(raw, str):
        try:
            obj = json.loads(raw)
        except (ValueError, RecursionError):
            return raw, None
    if isinstance(obj, dict) and "output" in obj:
        text = obj.get("output")
        if text is None:
            text = ""
        elif not isinstance(text, str):
            text = json.dumps(text)
        meta = obj.get("metadata") if isinstance(obj.get("metadata"), dict) else {}
        code = meta.get("exit_code", obj.get("exit_code"))
        return text, (code if _is_int(code) else None)
    if raw is None:
        return "", None
    return (raw if isinstance(raw, str) else json.dumps(raw)), None


def _usage(raw: Any) -> Usage:
    """Codex counts cached tokens inside input_tokens and reasoning tokens inside
    output_tokens. Cached tokens move to cache_read; the rest map directly."""
    raw = raw if isinstance(raw, dict) else {}
    input_total = _count(raw.get("input_tokens"))
    cached = min(_count(raw.get("cached_input_tokens")), input_total)
    return Usage(input_tokens=input_total - cached,
                 output_tokens=_count(raw.get("output_tokens")),
                 cache_write_tokens=0,
                 cache_read_tokens=cached)


def _count(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return max(0, int(value))
    if isinstance(value, str) and value.strip().isdigit():
        return int(value)
    return 0


def _split(u: Usage, n: int) -> List[Usage]:
    """Divide u into n parts whose sums equal u exactly."""
    parts = [Usage() for _ in range(n)]
    for f in USAGE_FIELDS:
        q, r = divmod(getattr(u, f), n)
        for i, part in enumerate(parts):
            setattr(part, f, q + (1 if i < r else 0))
    return parts


def _minus(a: Usage, b: Usage) -> Usage:
    return Usage(*(max(0, getattr(a, f) - getattr(b, f)) for f in USAGE_FIELDS))
