"""Plain-language step summaries.

Default: deterministic templates (free, offline).
With --ai: one batched call to a cheap Claude model (Haiku 5.5 by default) that
rewrites every step and writes a 2-3 sentence overview of the run. The call goes
through llm.py, so every text is redacted before it is sent.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from . import llm
from .parser import Run, Step, Usage
from .paths import is_abs, relative
from .shell_writes import shell_deletes, shell_writes

DEFAULT_AI_MODEL = os.environ.get("RUNLEDGER_SUMMARY_MODEL", "claude-haiku-5-5")


def _short(path: str, cwd: Optional[str]) -> str:
    return relative(path, cwd)


COMMAND_LIMIT = 120
GOAL_LIMIT = 200


def shorten(text: str, limit: int) -> str:
    """Cut at the last space before `limit` and add an ellipsis; one long word is cut hard."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    head = text[:limit - 1]
    space = head.rfind(" ")
    if space >= limit // 2:
        head = head[:space]
    return head.rstrip() + "…"


def _lines(s: Any) -> int:
    return len(s.splitlines()) if isinstance(s, str) else 0


def _str(value: Any) -> str:
    """A step input field as text; session files may carry any JSON type here."""
    return value if isinstance(value, str) else ""


def _edits(inp: Dict[str, Any]) -> List[Dict[str, Any]]:
    edits = inp.get("edits")
    if not isinstance(edits, list) or not edits:
        return [inp]
    return [e for e in edits if isinstance(e, dict)]


@dataclass
class FileChange:
    path: str
    added: int = 0
    removed: int = 0
    created: bool = False
    deleted: bool = False
    shell: bool = False      # written by a shell command, so the line counts may be incomplete
    verified: bool = False   # line counts come from the Git working tree
    binary: bool = False     # Git sees a binary file, so there are no line counts


def file_changes(run: Run) -> Dict[str, FileChange]:
    changes: Dict[str, FileChange] = {}
    for s in run.steps:
        p = _str(s.input.get("file_path")) or _str(s.input.get("notebook_path"))
        p = _short(p, run.cwd) if p else p
        if s.tool == "Write" and p:
            fc = changes.setdefault(p, FileChange(p))
            if not fc.added and not fc.removed:
                fc.created = "created" in _str(s.result_text).lower() or not s.result_text
            fc.added += _lines(s.input.get("content"))
        elif s.tool in ("Edit", "MultiEdit") and p:
            fc = changes.setdefault(p, FileChange(p))
            for e in _edits(s.input):
                o, n = e.get("old_string"), e.get("new_string")
                fc.removed += _lines(o)
                fc.added += _lines(n)
        elif s.tool == "Delete" and p:
            changes.setdefault(p, FileChange(p)).deleted = True
        elif s.tool == "NotebookEdit" and p:
            changes.setdefault(p, FileChange(p)).added += _lines(s.input.get("new_source"))
        elif s.tool == "Bash":
            cmd = _str(s.input.get("command"))
            for target in shell_deletes(cmd):
                p = _short(target, run.cwd)
                if run.cwd and is_abs(p):
                    continue  # outside the project, such as a scratch file in /tmp
                fc = changes.setdefault(p, FileChange(p))
                fc.deleted = fc.shell = True
            for w in shell_writes(cmd):
                p = _short(w.path, run.cwd)
                if run.cwd and is_abs(p):
                    continue  # outside the project, such as a backup in /tmp
                fc = changes.setdefault(p, FileChange(p))
                fc.shell = True
                fc.deleted = w.deleted
                fc.added += w.added
    for p, fc in changes.items():
        git = run.git_changes.get(p) if fc.shell else None
        if git:
            fc.added, fc.removed = int(git.get("added") or 0), int(git.get("removed") or 0)
            fc.created = git.get("status") == "untracked"
            fc.deleted = git.get("status") == "deleted"
            fc.verified = True
            fc.binary = bool(git.get("binary"))
    return changes


def template_summary(step: Step, cwd: Optional[str]) -> str:
    i, t = step.input, step.tool
    p = _short(_str(i.get("file_path")) or _str(i.get("notebook_path")) or _str(i.get("path")), cwd)
    if t == "Read":
        return f"Read {p}"
    if t == "Delete":
        return f"Deleted {p}"
    if t == "Search":
        return f"Searched for '{_str(i.get('pattern'))}'" + (f" in {p}" if p else "")
    if t == "Write":
        return f"Wrote {p} ({_lines(i.get('content'))} lines)"
    if t in ("Edit", "MultiEdit"):
        edits = _edits(i)
        a = sum(_lines(e.get("new_string")) for e in edits)
        r = sum(_lines(e.get("old_string")) for e in edits)
        return f"Edited {p} (+{a} −{r})"
    if t == "Bash":
        desc = _str(i.get("description"))
        lines = _str(i.get("command")).strip().splitlines() or [""]
        # Redact before cutting, so the 120-character limit cannot keep half of a secret.
        # A backtick would close the Markdown code span and split the HTML <code> early.
        cmd = shorten(llm.redact(lines[0]), COMMAND_LIMIT).replace("`", "'")
        if len(lines) > 1:
            cmd += f" (+{len(lines) - 1} line{'s' if len(lines) > 2 else ''})"
        outcome = " — failed" if step.is_error else ""
        m = re.search(r"(\d+)\s+passed", step.result_text)
        f = re.search(r"(\d+)\s+failed", step.result_text)
        if m or f:
            bits = []
            if f:
                bits.append(f"{f.group(1)} failed")
            if m:
                bits.append(f"{m.group(1)} passed")
            outcome = " — tests: " + ", ".join(bits)
        return f"{desc or 'Ran'}: `{cmd}`{outcome}" if desc else f"Ran `{cmd}`{outcome}"
    if t == "Grep":
        return f"Searched the code for “{_str(i.get('pattern'))}”"
    if t == "Glob":
        return f"Listed files matching {_str(i.get('pattern'))}"
    if t == "LS":
        return f"Listed folder {p or '.'}"
    if t == "WebFetch":
        return f"Fetched {_str(i.get('url'))}"
    if t == "WebSearch":
        return f"Searched the web for “{_str(i.get('query'))}”"
    if t == "TodoWrite":
        return "Updated its to-do list"
    if t in ("Task", "Agent"):
        return f"Delegated to a sub-agent: {_str(i.get('description'))}"
    if t == "NotebookEdit":
        return f"Edited notebook {p}"
    if t.startswith("mcp__"):
        parts = t.split("__")
        return f"Called {parts[-1]} on MCP server {parts[1] if len(parts) > 2 else ''}".strip()
    return f"Used tool {t}"


def apply_templates(run: Run) -> None:
    for s in run.steps:
        s.summary = template_summary(s, run.cwd)
    if not run.overall_summary:
        n_files = len(file_changes(run))
        goal = shorten(llm.redact(run.prompts[0]), GOAL_LIMIT) if run.prompts else "an unspecified task"
        goal = goal.rstrip(" .")
        run.overall_summary = (f"Asked to: “{goal}”. The agent took {len(run.steps)} steps "
                               f"and changed {n_files} file{'s' if n_files != 1 else ''}.")


# ---------------- AI summaries (Claude API) ----------------

def _clip(text: Optional[str], limit: int) -> str:
    """Redact first, then cut. A secret is replaced whole, so the cut cannot leave half of it."""
    s = llm.redact(text or "")
    return s if len(s) <= limit else s[:limit] + f"…[+{len(s) - limit} chars]"


def _compact_step(s: Step) -> Dict:
    inp = {}
    for k, v in s.input.items():
        inp[k] = _clip(v, 600) if isinstance(v, str) else v
    return {"n": s.index, "tool": s.tool, "input": inp,
            "result": _clip(s.result_text, 400), "error": s.is_error}


def ai_summaries(run: Run, api_key: Optional[str] = None, model: str = DEFAULT_AI_MODEL,
                 timeout: int = 120) -> Usage:
    """Rewrite step summaries with Claude. Returns the token usage it spent.
    Raises llm.LLMError (a RuntimeError) when the call fails."""
    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("Set ANTHROPIC_API_KEY to use --ai summaries.")
    steps = [_compact_step(s) for s in run.steps]
    requests = json.dumps([_clip(p, 1000) for p in run.prompts[:3]])[:2000]
    prompt = (
        "You write receipts for AI coding-agent runs for busy reviewers who may not be engineers.\n"
        "For each step, write ONE short plain-English sentence (max 18 words) saying what the agent did "
        "and why it matters. Name files by their short path. No speculation.\n"
        "Then write a 2-3 sentence overview of the whole run: goal, what changed, outcome.\n"
        "Return ONLY JSON: {\"overview\": str, \"steps\": [{\"n\": int, \"summary\": str}]}\n\n"
        f"Working folder: {_clip(run.cwd, 300)}\nUser request(s): {requests}\n"
        f"Agent's final message: {_clip(run.final_message, 1500)}\n"
        f"Steps: {json.dumps(steps)[:60000]}"
    )
    data = llm.call([{"role": "user", "content": prompt}], model, max_tokens=4000,
                    api_key=api_key, timeout=timeout)
    text = llm.text_of(data)
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise RuntimeError("Claude did not return JSON summaries.")
    parsed = json.loads(m.group(0))
    by_n = {int(x["n"]): x["summary"] for x in parsed.get("steps", []) if "n" in x}
    for s in run.steps:
        if s.index in by_n and by_n[s.index].strip():
            s.summary = by_n[s.index].strip()
    if parsed.get("overview"):
        run.overall_summary = parsed["overview"].strip()
    return llm.usage(data)
