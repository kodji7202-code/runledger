"""The hook file each agent reads, and the structure it expects.

`runledger guard install|uninstall --agent claude-code|droid|codex|all` writes one file per
agent, per scope:

  Claude Code   `<project>/.claude/settings.json`, `~/.claude/settings.json`
                (`CLAUDE_CONFIG_DIR` moves the user file)
                `{"hooks": {Event: [{"matcher", "hooks": [...]}]}}`
  Factory Droid `<project>/.factory/hooks.json`, `<factory dir>/hooks.json`
                (`FACTORY_HOME` names that directory, `FACTORY_HOME_OVERRIDE` a home whose
                config directory is `<override>/.factory`; the default is `~/.factory`)
                `{Event: [{"matcher", "hooks": [...]}]}` - keyed directly by event, with no
                `hooks` wrapper, and never `settings.json`
  Codex CLI     `<project>/.codex/hooks.json`, `<codex home>/hooks.json` (`CODEX_HOME`)
                `{"hooks": {Event: [{"matcher", "hooks": [...]}]}}`

Reading and writing the JSON lives here too (`read_settings`/`write_settings`), so the installer
and the doctor share one implementation of the empty-file, BOM, backup and atomic-write rules.
"""
from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

AGENT_CLAUDE = "claude-code"
AGENT_DROID = "droid"
AGENT_CODEX = "codex"
AGENTS = (AGENT_CLAUDE, AGENT_DROID, AGENT_CODEX)
AGENT_ALL = "all"
AGENT_CHOICES = AGENTS + (AGENT_ALL,)

# The top-level key the events live under: Codex and Claude Code wrap, Droid does not.
WRAP_KEY = {AGENT_CLAUDE: "hooks", AGENT_DROID: None, AGENT_CODEX: "hooks"}
# The PreToolUse matcher: `*` for Claude Code and Droid (regex, "all tools"), a regex for
# Codex, whose matcher is matched against the tool name.
MATCHER = {AGENT_CLAUDE: "*", AGENT_DROID: "*", AGENT_CODEX: ".*"}
LABEL = {AGENT_CLAUDE: "Claude Code", AGENT_DROID: "Droid", AGENT_CODEX: "Codex"}
PROJECT_FOLDER = {AGENT_CLAUDE: ".claude", AGENT_DROID: ".factory", AGENT_CODEX: ".codex"}
FILE_NAME = {AGENT_CLAUDE: "settings.json", AGENT_DROID: "hooks.json", AGENT_CODEX: "hooks.json"}
# The variable that moves the agent's config directory, and the command that creates it.
CONFIG_ENV = {AGENT_DROID: "FACTORY_HOME", AGENT_CODEX: "CODEX_HOME"}
CLI_NAME = {AGENT_DROID: "droid", AGENT_CODEX: "codex"}


class InstallError(Exception):
    """A hook file that cannot be read or written. Nothing was changed."""


@dataclass(frozen=True)
class HookTarget:
    """One agent's hook file: where it is and how its events are shaped."""

    agent: str
    path: Path
    wrap_key: Optional[str]
    matcher: str

    @property
    def label(self) -> str:
        return LABEL[self.agent]


def home_dir(home: Optional[Path] = None) -> Path:
    """The home folder to resolve defaults against; the caller may pass one explicitly."""
    if home is not None:
        return Path(home)
    base = os.environ.get("HOME") or os.environ.get("USERPROFILE")
    return Path(base) if base else Path.home()


def claude_dir(home: Optional[Path] = None) -> Path:
    """`CLAUDE_CONFIG_DIR` when set, else `<home>/.claude`."""
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    return home_dir(home) / ".claude"


def factory_dir(home: Optional[Path] = None) -> Path:
    """Factory's config directory, in the order the droid binary resolves it: `FACTORY_HOME`
    names that directory directly, `FACTORY_HOME_OVERRIDE` names a home whose config directory
    is `<override>/.factory`, and without either one it is `<home>/.factory`."""
    base = os.environ.get("FACTORY_HOME")
    if base:
        return Path(base).expanduser()
    override = os.environ.get("FACTORY_HOME_OVERRIDE")
    if override:
        return Path(override).expanduser() / ".factory"
    return home_dir(home) / ".factory"


def codex_dir(home: Optional[Path] = None) -> Path:
    """`CODEX_HOME` when set, else `<home>/.codex`."""
    base = os.environ.get("CODEX_HOME")
    return Path(base).expanduser() if base else home_dir(home) / ".codex"


_DIRECTORY = {AGENT_CLAUDE: claude_dir, AGENT_DROID: factory_dir, AGENT_CODEX: codex_dir}


def agent_dir(agent: str, home: Optional[Path] = None) -> Path:
    """The agent's own config directory, the one a global install writes under."""
    return _DIRECTORY[agent](home)


def target_at(agent: str, path: Any) -> HookTarget:
    """The target for one agent at an explicit file path."""
    return HookTarget(agent=agent, path=Path(path), wrap_key=WRAP_KEY[agent], matcher=MATCHER[agent])


def target_for(agent: str, project_dir: Any = None, global_scope: bool = False,
               home: Optional[Path] = None) -> HookTarget:
    """The hook file of one agent. Project scope writes under `project_dir` (the caller
    resolves the project root); global scope under the agent's own config directory."""
    if global_scope:
        base = agent_dir(agent, home)
    else:
        base = Path(project_dir) / PROJECT_FOLDER[agent]
    return target_at(agent, base / FILE_NAME[agent])


def plan_targets(agent_value: str, project_dir: Any = None, global_scope: bool = False,
                 home: Optional[Path] = None) -> Tuple[List[HookTarget], List[Tuple[str, str]]]:
    """The targets to write, plus `(agent, reason)` for every agent `all` skipped.

    `all` never creates a global config directory from nothing: an agent whose directory does
    not exist is reported and skipped. Claude Code is always installed, as it was before the
    flag existed, and a project install is explicit about its folder either way."""
    if agent_value != AGENT_ALL:
        return [target_for(agent_value, project_dir, global_scope, home)], []
    targets: List[HookTarget] = []
    skipped: List[Tuple[str, str]] = []
    for agent in AGENTS:
        target = target_for(agent, project_dir, global_scope, home)
        if global_scope and agent != AGENT_CLAUDE and not target.path.parent.is_dir():
            skipped.append((agent, f"no {LABEL[agent]} folder at {target.path.parent}: run "
                                   f"{CLI_NAME[agent]} once to create it, or set {CONFIG_ENV[agent]}."))
            continue
        targets.append(target)
    return targets, skipped


def read_settings(path: Path) -> Tuple[bool, Dict[str, Any]]:
    """`(existed, data)`. A missing or empty file gives `{}`; a UTF-8 BOM is accepted.
    Raises InstallError when the file does not hold a JSON object."""
    path = Path(path)
    if not path.exists():
        return False, {}
    text = path.read_text(encoding="utf-8-sig")
    if not text.strip():
        return True, {}
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise InstallError(f"{path} is not valid JSON ({exc}); fix or move it. Nothing was changed.") from None
    if not isinstance(data, dict):
        raise InstallError(f"{path} must contain a JSON object. Nothing was changed.")
    return True, data


def write_settings(path: Path, data: Dict[str, Any], existed: bool) -> Optional[Path]:
    """Write atomically, after copying an existing file to `<name>.bak`. Returns the backup."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if existed:
        backup = path.with_name(path.name + ".bak")
        shutil.copy2(path, backup)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return backup
