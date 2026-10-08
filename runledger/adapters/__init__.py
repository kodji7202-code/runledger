"""Agent adapters: turn each coding agent's session logs into a common Run.

Every adapter module exposes:
  NAME: str                                   e.g. "codex"
  LABEL: str                                  e.g. "Codex CLI"
  detect(path: Path) -> bool                  cheap check: is this file one of ours?
  parse(path: Path) -> Run                    full parse into parser.Run / parser.Step
  find_sessions(project: Optional[str]) -> List[Path]
                                              newest first; project=None means all projects

Steps must use the canonical tool vocabulary so risk scoring and receipts
work unchanged for every agent:
  Bash        input {"command": str}                    (any shell, incl. PowerShell)
  PowerShell  input {"command": str}
  Read        input {"file_path": str}
  Write       input {"file_path": str, "content": str}
  Edit        input {"file_path": str, "old_string": str, "new_string": str}
  MultiEdit   input {"file_path": str, "edits": [{"old_string", "new_string"}]}
  Delete      input {"file_path": str}
  WebFetch    input {"url": str}
  Search      input {"pattern": str, "path": str}
  mcp__<server>__<tool>  input as given
Anything else keeps the agent's own tool name.
"""
from __future__ import annotations

import importlib
from pathlib import Path
from typing import List, Optional

ADAPTER_MODULES = ["claude_code", "codex", "aider", "native"]


def adapters():
    out = []
    for name in ADAPTER_MODULES:
        try:
            out.append(importlib.import_module(f"{__name__}.{name}"))
        except ImportError:
            continue
    return out


def get(name: str):
    for a in adapters():
        if a.NAME == name:
            return a
    raise KeyError(f"unknown agent '{name}' (known: {', '.join(a.NAME for a in adapters())})")


def label(name: str) -> str:
    """Display name for an agent id: "claude-code" -> "Claude Code". Ids without an
    adapter (for example a native file's own "agent" value) are shown as given."""
    try:
        return get(name).LABEL
    except KeyError:
        return name


def detect(path) -> object:
    """Return the adapter for a session file; Claude Code is the fallback."""
    p = Path(path)
    for a in adapters():
        if a.NAME != "claude-code" and a.detect(p):
            return a
    return get("claude-code")


def find_sessions(project: Optional[str] = None, agent: Optional[str] = None) -> List[Path]:
    chosen = [get(agent)] if agent else adapters()
    found: List[Path] = []
    for a in chosen:
        found.extend(a.find_sessions(project))
    return sorted(set(found), key=lambda f: f.stat().st_mtime, reverse=True)
