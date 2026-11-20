"""Tolerant JSONL object reading for agent session files."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterator


def read_objects(path: Path, *, strip_lines: bool = False) -> Iterator[Dict[str, Any]]:
    """Skip invalid/non-object lines; optionally accept surrounding Unicode whitespace."""
    with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
        for line in fh:
            if strip_lines:
                line = line.strip()
                if not line:
                    continue
            try:
                obj = json.loads(line)
            except (ValueError, RecursionError):
                continue
            if isinstance(obj, dict):
                yield obj
