"""Plain-language step summaries.

Default: deterministic templates (free, offline).
With --ai: one batched call to a cheap Claude model (Haiku by default) that
rewrites every step and writes a 2-3 sentence overview of the run.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Dict, List, Optional

from .parser import Run, Step, Usage

DEFAULT_AI_MODEL = os.environ.get("RUNLEDGER_SUMMARY_MODEL", "claude-haiku-4-5")


def _short(path: str, cwd: Optional[str]) -> str:
    if cwd and path.startswith(cwd.rstrip("/") + "/"):
        return path[len(cwd.rstrip("/")) + 1:]
    return path


def _lines(s: Optional[str]) -> int:
    return len(s.splitlines()) if s else 0


@dataclass
class FileChange:
    path: str
    added: int = 0
    removed: int = 0
    created: bool = False
    deleted: bool = False


def file_changes(run: Run) -> Dict[str, FileChange]:
    changes: Dict[str, FileChange] = {}
    for s in run.steps:
        p = s.input.get("file_path") or s.input.get("notebook_path")
        p = _short(p, run.cwd) if p else p
        if s.tool == "Write" and p:
            fc = changes.setdefault(p, FileChange(p))
            if not fc.added and not fc.removed:
                fc.created = "created" in s.result_text.lower() or not s.result_text
            fc.added += _lines(s.input.get("content"))
        elif s.tool in ("Edit", "MultiEdit") and p:
            fc = changes.setdefault(p, FileChange(p))
            for e in s.input.get("edits") or [s.input]:
                o, n = e.get("old_string") or "", e.get("new_string") or ""
                fc.removed += _lines(o)
                fc.added += _lines(n)
        elif s.tool == "Delete" and p:
            changes.setdefault(p, FileChange(p)).deleted = True
        elif s.tool == "NotebookEdit" and p:
            changes.setdefault(p, FileChange(p)).added += _lines(s.input.get("new_source"))
        elif s.tool == "Bash":
            cmd = str(s.input.get("command", ""))
            m = re.match(r"^\s*(git\s+rm|rm)\s+(?:-\S+\s+)*(.+)$", cmd)
            if m:
                for t in m.group(2).split():
                    if not t.startswith("-") and not any(c in t for c in "|;&>"):
                        changes.setdefault(t, FileChange(t)).deleted = True
    return changes


def template_summary(step: Step, cwd: Optional[str]) -> str:
    i, t = step.input, step.tool
    p = _short(str(i.get("file_path") or i.get("notebook_path") or i.get("path") or ""), cwd)
    if t == "Read":
        return f"Read {p}"
    if t == "Delete":
        return f"Deleted {p}"
    if t == "Search":
        return f"Searched for '{i.get('pattern', '')}'" + (f" in {p}" if p else "")
    if t == "Write":
        return f"Wrote {p} ({_lines(i.get('content'))} lines)"
    if t in ("Edit", "MultiEdit"):
        edits = i.get("edits") or [i]
        a = sum(_lines(e.get("new_string")) for e in edits)
        r = sum(_lines(e.get("old_string")) for e in edits)
        return f"Edited {p} (+{a} −{r})"
    if t == "Bash":
        desc = i.get("description")
        cmd = str(i.get("command", "")).strip().splitlines()[0][:120]
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
        return f"Searched the code for “{i.get('pattern', '')}”"
    if t == "Glob":
        return f"Listed files matching {i.get('pattern', '')}"
    if t == "LS":
        return f"Listed folder {p or '.'}"
    if t == "WebFetch":
        return f"Fetched {i.get('url', '')}"
    if t == "WebSearch":
        return f"Searched the web for “{i.get('query', '')}”"
    if t == "TodoWrite":
        return "Updated its to-do list"
    if t in ("Task", "Agent"):
        return f"Delegated to a sub-agent: {i.get('description', '')}"
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
        goal = run.prompts[0][:200] if run.prompts else "an unspecified task"
        goal = goal.rstrip(" .")
        run.overall_summary = (f"Asked to: “{goal}”. The agent took {len(run.steps)} steps "
                               f"and changed {n_files} file{'s' if n_files != 1 else ''}.")


# ---------------- AI summaries (Claude API) ----------------

def _compact_step(s: Step) -> Dict:
    inp = {}
    for k, v in s.input.items():
        if isinstance(v, str):
            inp[k] = v if len(v) <= 600 else v[:600] + f"…[+{len(v) - 600} chars]"
        else:
            inp[k] = v
    return {"n": s.index, "tool": s.tool, "input": inp,
            "result": (s.result_text or "")[:400], "error": s.is_error}


def ai_summaries(run: Run, api_key: Optional[str] = None, model: str = DEFAULT_AI_MODEL,
                 timeout: int = 120) -> Usage:
    """Rewrite step summaries with Claude. Returns the token usage it spent."""
    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("Set ANTHROPIC_API_KEY to use --ai summaries.")
    steps = [_compact_step(s) for s in run.steps]
    prompt = (
        "You write receipts for AI coding-agent runs for busy reviewers who may not be engineers.\n"
        "For each step, write ONE short plain-English sentence (max 18 words) saying what the agent did "
        "and why it matters. Name files by their short path. No speculation.\n"
        "Then write a 2-3 sentence overview of the whole run: goal, what changed, outcome.\n"
        "Return ONLY JSON: {\"overview\": str, \"steps\": [{\"n\": int, \"summary\": str}]}\n\n"
        f"Working folder: {run.cwd}\nUser request(s): {json.dumps(run.prompts[:3])[:2000]}\n"
        f"Agent's final message: {run.final_message[:1500]}\n"
        f"Steps: {json.dumps(steps)[:60000]}"
    )
    body = json.dumps({
        "model": model, "max_tokens": 4000,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body, method="POST",
        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Claude API error {e.code}: {e.read().decode()[:300]}") from e
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
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
    u = data.get("usage", {})
    return Usage(int(u.get("input_tokens", 0)), int(u.get("output_tokens", 0)))
