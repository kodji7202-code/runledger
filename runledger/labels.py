"""Whole-path labels for spaced drive paths, applied when output is rendered and never before.

`E:\\Work\\Client X\\secret.txt` is one path to a reader and two words to a shell. Every decision
RunLedger takes - findings and their de-duplication, severities, user policies and ignore rules,
guard rules and decisions, containment, file keys, deletion state, Git lookups and counts - reads
the shell's own words, and `paths.spaced_drive_labels` is the one reading of the display rule.
This module maps that rule onto the text a receipt, a push payload or a guard message shows, so a
finding names the folder its spaced path belongs to and safe mode can mask it whole (VAL-CORE-034).
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any, Dict, List, Optional, Sequence

from . import paths
from .ps_writes import shell_kind
from .risk import SHELL_TOOLS, _strip_heredocs


def command_labels(tool: str, command: Any) -> Dict[str, str]:
    """The labels of one command: only a shell command can hold a spaced drive path."""
    if tool not in SHELL_TOOLS or not isinstance(command, str):
        return {}
    return paths.spaced_drive_labels(_strip_heredocs(command, shell_kind(tool, command) == "powershell"))


def step_labels(step: Any) -> Dict[str, str]:
    """The labels of one step's command (heredoc and here-string bodies are not commands)."""
    return command_labels(str(getattr(step, "tool", "") or ""),
                          (getattr(step, "input", None) or {}).get("command"))


def step_label_map(run: Any) -> Dict[int, Dict[str, str]]:
    """The labels of every step of `run`, by step index, for the findings it shows."""
    out: Dict[int, Dict[str, str]] = {}
    for step in getattr(run, "steps", None) or []:
        labels = step_labels(step)
        if labels:
            out[step.index] = labels
    return out


def show_reason(reason: str, labels: Optional[Dict[str, str]]) -> str:
    """`reason` with every leading fragment of a spaced drive path written whole."""
    return paths.show_spaced_drive_paths(reason, labels) if labels else reason


def show_risks(risks: Sequence[Any], by_step: Dict[int, Dict[str, str]]) -> List[Any]:
    """Copies of `risks` with the whole spaced drive path in every reason that names a fragment.

    Presentation only: the caller renders these, while the findings, the policies, the guard
    decisions and the file keys keep the reading the shell's own words gave them."""
    if not risks:
        return list(risks)
    if not by_step:
        return list(risks)
    out = []
    for r in risks:
        labels = by_step.get(getattr(r, "step", None))
        out.append(replace(r, reason=show_reason(r.reason, labels)) if labels else r)
    return out
