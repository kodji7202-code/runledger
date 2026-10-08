"""Claude Code adapter (the original parser)."""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from .. import parser

NAME = "claude-code"
LABEL = "Claude Code"


def detect(path: Path) -> bool:
    return path.suffix == ".jsonl"


def parse(path: Path) -> parser.Run:
    run = parser.parse_session(path)
    run.agent = NAME
    return run


def find_sessions(project: Optional[str] = None) -> List[Path]:
    return parser.find_sessions(project)
