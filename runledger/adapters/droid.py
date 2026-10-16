"""Factory Droid JSONL sessions and their sibling .settings.json metadata.

FACTORY_HOME defaults to ~/.factory. Project folders under sessions replace
path separators with dashes. Messages carry modelId and tool_use/tool_result
blocks; settings.tokenUsage holds cumulative counts, not per-call usage.
"""
from __future__ import annotations

import json
import ntpath
import os
import posixpath
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from ..parser import Run, Step, Usage, _split_usage
from ..paths import _norm as _norm_style, join as _join
from ..pricing import UNATTRIBUTED
from .codex import patch_to_steps

NAME = "droid"
LABEL = "Factory Droid"
RESULT_LIMIT = 4000
_USAGE_FIELDS = ("input_tokens", "output_tokens", "cache_write_tokens", "cache_read_tokens")
_CONTEXT = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")
# The header id becomes part of a glob, so it must not carry separators or glob syntax.
_SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
# Droid's session context lists `% pwd` and its output, the absolute session folder, on the next line.
_PWD = re.compile(r"^% pwd[ \t]*\r?\n[ \t]*((?:/|[A-Za-z]:[\\/])[^\r\n]*?)[ \t]*$", re.M)
_SETTINGS_CWD_KEYS = ("cwd", "workingDirectory", "workspaceRoot", "projectRoot", "projectPath")
_PATH_KEYS = ("file_path", "notebook_path", "path")
_MAX_PROBED_DIRS = 50
_MAX_DECODE_PROBES = 2000


def factory_home() -> Path:
    base = os.environ.get("FACTORY_HOME")
    return Path(base).expanduser() if base else Path.home() / ".factory"


def _header(path: Path) -> Dict[str, Any]:
    """Read only a bounded header, never the full transcript, during discovery."""
    try:
        with path.open(encoding="utf-8-sig", errors="replace") as fh:
            for _ in range(8):
                line = fh.readline(65536)
                if not line:
                    break
                if line.strip():
                    obj = json.loads(line)
                    return obj if isinstance(obj, dict) else {}
    except (OSError, ValueError, RecursionError):
        pass
    return {}


def detect(path: Path) -> bool:
    p = Path(path)
    if p.suffix != ".jsonl":
        return False
    return _is_header(_header(p))


def _is_header(header: Dict[str, Any]) -> bool:
    return (header.get("type") == "session_start"
            and isinstance(header.get("id"), str) and isinstance(header.get("cwd"), str))


def _absolute(project: str) -> str:
    project = os.path.expanduser(project)
    return project if project.startswith(("/", "\\")) or _DRIVE.match(project) else os.path.abspath(project)


def _norm(path: str) -> str:
    path = posixpath.normpath(path.replace("\\", "/"))
    return path.lower() if sys.platform == "win32" or _DRIVE.match(path) else path


def _is_absolute(path: str) -> bool:
    return path.startswith(("/", "\\")) or bool(_DRIVE.match(path))


def _encode(cwd: str) -> str:
    """The session folder name Droid uses for a working folder. A colon cannot be part of a
    Windows file name, so the drive of 'C:\\x' is written 'C-' and the folder is 'C--x'."""
    if _DRIVE.match(cwd):
        cwd = cwd[0] + "-" + cwd[2:]
    return cwd.replace("\\", "-").replace("/", "-")


def _is_project_folder(name: str, project: str) -> bool:
    """True if a session folder `name` stands for the absolute folder `project`. Windows names
    are compared without case, and besides 'C--x' the drive may be written 'C:-x' (a session
    folder made on a POSIX host) or left out ('-x')."""
    win = sys.platform == "win32" or bool(_DRIVE.match(project))
    norm = _norm_style(project, win)
    names = {_encode(norm)}
    if _DRIVE.match(norm):
        names.update((norm[:2] + _encode(norm[2:]), _encode(norm[2:])))
    if win:
        return name.lower() in {n.lower() for n in names}
    return name in names


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def find_sessions(project: Optional[str] = None) -> List[Path]:
    root = factory_home() / "sessions"
    wanted = _absolute(str(project)) if project else None
    found = []
    for path in root.glob("*/*.jsonl"):
        if not path.is_file():
            continue
        header = _header(path)
        if not _is_header(header):
            continue
        # Encoding is lossy: /demo/a-b and /demo/a/b share a directory.
        cwd = header["cwd"]
        if wanted and _is_absolute(cwd) and _norm(cwd) != _norm(wanted):
            continue
        if wanted and not _is_absolute(cwd) and not _is_project_folder(path.parent.name, wanted):
            continue  # `droid exec --cwd .` records ".": the folder name is the only clue
        found.append(path)
    return sorted(found, key=_mtime, reverse=True)


def _objects(path: Path) -> Iterator[Dict[str, Any]]:
    with path.open(encoding="utf-8-sig", errors="replace") as fh:
        for line in fh:
            try:
                obj = json.loads(line)
            except (ValueError, RecursionError):
                continue  # tolerate partial writes in a live session
            if isinstance(obj, dict):
                yield obj


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(_text(b.get("text")) for b in value
                         if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _usage(raw: Dict[str, Any]) -> Usage:
    def count(*names: str) -> int:
        for name in names:
            value = raw.get(name)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                return value
        return 0

    # Input counts exclude cache tokens. Thinking tokens are already part of
    # output, and credits are not tokens. Neither is added a second time.
    return Usage(
        count("inputTokens", "input_tokens"),
        count("outputTokens", "output_tokens"),
        count("cacheCreationTokens", "cache_creation_input_tokens"),
        count("cacheReadTokens", "cache_read_input_tokens"),
    )


def _tools(name: str, inp: Dict[str, Any], depth: int = 0) -> List[Tuple[str, Dict[str, Any]]]:
    if name.startswith("functions."):
        name = name[len("functions."):]
    if name == "multi_tool_use.parallel" and depth < 8:
        out = []
        calls = inp.get("tool_uses")
        for call in calls if isinstance(calls, list) else []:
            if isinstance(call, dict) and isinstance(call.get("recipient_name"), str):
                params = call.get("parameters")
                out.extend(_tools(call["recipient_name"], params if isinstance(params, dict) else {}, depth + 1))
        return out
    if name in ("ApplyPatch", "apply_patch"):
        patch = _text(inp.get("input") or inp.get("patch"))
        return patch_to_steps(patch) or [(name, inp)]
    if name == "Execute":
        return [("Bash", {"command": _text(inp.get("command"))})]
    if name == "Create":
        return [("Write", {"file_path": _text(inp.get("file_path")), "content": _text(inp.get("content"))})]
    if name == "Edit":
        return [("Edit", {
            "file_path": _text(inp.get("file_path")),
            "old_string": _text(inp.get("old_str", inp.get("old_string"))),
            "new_string": _text(inp.get("new_str", inp.get("new_string"))),
        })]
    if name in ("Grep", "Glob", "LS"):
        pattern = inp.get("pattern", inp.get("patterns", "*"))
        if isinstance(pattern, list):
            pattern = " | ".join(p for p in pattern if isinstance(p, str))
        return [("Search", {"pattern": _text(pattern),
                            "path": _text(inp.get("path") or inp.get("folder") or inp.get("directory_path"))})]
    if name == "FetchUrl":
        return [("WebFetch", {"url": _text(inp.get("url"))})]
    if "__" in name and not name.startswith("mcp__"):
        name = "mcp__" + name
    return [(name, inp)]


def _settings_path(path: Path, header: Dict[str, Any]) -> Optional[Path]:
    """The sibling settings file, or the one Droid keeps for this session id under FACTORY_HOME.
    The fallback covers transcripts that were copied or renamed without their settings file."""
    sibling = path.with_suffix(".settings.json")
    if sibling.is_file():
        return sibling
    sid = header.get("id")
    if not isinstance(sid, str) or not _SESSION_ID.fullmatch(sid):
        return None
    name = f"{sid}.settings.json"
    root = factory_home() / "sessions"
    cwd = header.get("cwd")
    folder = _encode(cwd) if isinstance(cwd, str) else ""
    if folder not in ("", ".", ".."):
        preferred = root / folder / name
        if preferred.is_file():
            return preferred
    try:
        for candidate in sorted(root.glob(f"*/{name}")):
            if candidate.is_file():
                return candidate
    except OSError:
        pass
    return None


def _minus(a: Usage, b: Usage) -> Usage:
    return Usage(*(max(0, getattr(a, f) - getattr(b, f)) for f in _USAGE_FIELDS))


def _child_model(settings_path: Optional[Path], child_id: Any) -> Optional[str]:
    if settings_path is None or not isinstance(child_id, str) or not _SESSION_ID.fullmatch(child_id):
        return None
    try:
        child = json.loads((settings_path.parent / f"{child_id}.settings.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(child, dict):
        return None
    return _text(child.get("model")) or None


def _child_usage(settings: Dict[str, Any], own: Usage, settings_path: Optional[Path]) -> Dict[str, Usage]:
    """Sub-agent usage: inclusiveTokenUsage minus the session's own tokenUsage. Each child
    session is credited to the model in its own settings file when that file is found;
    the rest is unattributed, never priced as the parent's model."""
    raw = settings.get("inclusiveTokenUsage")
    if not isinstance(raw, dict):
        return {}
    inclusive = _usage(raw)
    if any(getattr(inclusive, f) < getattr(own, f) for f in _USAGE_FIELDS):
        return {}  # an inclusive total below the session's own usage is not a superset; ignore it
    extra = _minus(inclusive, own)
    if not extra.total:
        return {}
    out: Dict[str, Usage] = {}
    by_child = settings.get("childInclusiveTokenUsageBySessionId")
    for child_id, child_raw in (by_child.items() if isinstance(by_child, dict) else []):
        model = _child_model(settings_path, child_id)
        if not model or not isinstance(child_raw, dict):
            continue
        share = Usage(*(min(getattr(_usage(child_raw), f), getattr(extra, f)) for f in _USAGE_FIELDS))
        out.setdefault(model, Usage()).add(share)
        extra = _minus(extra, share)
    if extra.total:
        out.setdefault(UNATTRIBUTED, Usage()).add(extra)
    return out


def _decode_folder(name: str) -> Optional[str]:
    """The existing directory an encoded session folder name stands for, or None.
    A '-' is either a separator or part of a name, so the filesystem decides: longer names
    are tried first, and only a candidate that exists as a directory is accepted."""
    if name.startswith("-"):
        root, rest, pathmod = "/", name[1:], posixpath
    elif re.match(r"^[A-Za-z][:-]-", name):
        root, rest, pathmod = name[0] + ":\\", name[3:], ntpath
    else:
        return None
    tokens = rest.split("-")
    if not rest or len(tokens) > 64:
        return None
    probes = [0]

    def walk(base: str, i: int) -> Optional[str]:
        if i == len(tokens):
            return base
        for j in range(len(tokens), i, -1):
            part = "-".join(tokens[i:j])
            if not part or part in (".", ".."):
                continue
            probes[0] += 1
            if probes[0] > _MAX_DECODE_PROBES:
                return None
            candidate = pathmod.join(base, part)
            if os.path.isdir(candidate):
                found = walk(candidate, j)
                if found:
                    return found
        return None

    return walk(root, 0)


def _tool_root(steps: List[Step]) -> Optional[str]:
    """The Git work tree that most absolute tool paths lie in, when it holds a majority of them."""
    from ..gitinfo import toplevel
    paths = [step.input.get(k) for step in steps for k in _PATH_KEYS]
    paths = [p for p in paths if isinstance(p, str) and _is_absolute(p)]
    if not paths:
        return None
    roots: Dict[str, Optional[str]] = {}
    votes: Dict[str, int] = {}
    for p in paths:
        key = p if os.path.isdir(p) else os.path.dirname(p)
        if key not in roots:
            if len(roots) >= _MAX_PROBED_DIRS:
                continue
            folder = key
            while folder and not os.path.isdir(folder) and os.path.dirname(folder) != folder:
                folder = os.path.dirname(folder)  # a file the run deleted, or a new folder
            roots[key] = toplevel(folder) if os.path.isdir(folder) else None
        top = roots.get(key)
        if top:
            votes[top] = votes.get(top, 0) + 1
    if not votes:
        return None
    best, count = max(votes.items(), key=lambda kv: kv[1])
    return best if count * 2 > len(paths) else None


def _resolve(base: str, relative: str) -> str:
    """`relative` (the recorded cwd) joined to `base`, unless `base` already ends with it."""
    rel = posixpath.normpath(relative.replace("\\", "/"))
    if rel in (".", ""):
        return base
    if _norm(base).endswith("/" + _norm(rel).strip("/")):
        return base
    return _join(base, rel)


def _recover_cwd(run: Run, recorded: str, pwd: Optional[str], settings: Dict[str, Any],
                 folder_name: str) -> Tuple[Optional[str], str]:
    """(cwd, source) for a log that records a relative working folder. Sources, in order:
    the session's own `% pwd` output, a settings field, the Git work tree of the tool paths,
    then the encoded session folder name. Nothing is resolved against RunLedger's own folder."""
    if pwd and _is_absolute(pwd):
        return _resolve(pwd, recorded), "session"
    for key in _SETTINGS_CWD_KEYS:
        value = settings.get(key)
        if isinstance(value, str) and _is_absolute(value):
            return _resolve(value, recorded), "settings"
    root = _tool_root(run.steps)
    if root:
        return _resolve(root, recorded), "session"
    decoded = _decode_folder(folder_name)
    if decoded:
        return _resolve(decoded, recorded), "session_dir"
    return recorded or None, "unknown"


def parse(path: Path) -> Run:
    p = Path(path)
    settings_path = _settings_path(p, _header(p))
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8-sig")) if settings_path else {}
    except (OSError, ValueError, RecursionError):
        settings = {}
    if not isinstance(settings, dict):
        settings = {}
    default_model = _text(settings.get("model")) or "unknown"
    run = Run(p.stem, str(p), None, None, None, None, [], [], "", Usage(), {}, agent=NAME)
    calls: Dict[str, List[Step]] = {}
    results: Dict[str, Tuple[str, bool]] = {}
    issued: Dict[str, List[Step]] = {}
    message_usage: Dict[str, Usage] = {}
    message_models: Dict[str, str] = {}
    observed_models = set()
    header_seen = False
    pwd: Optional[str] = None

    for obj in _objects(p):
        ts = _text(obj.get("timestamp")) or None
        if ts:
            run.started = run.started or ts
            run.ended = ts
        if obj.get("type") == "session_start" and not header_seen:
            header_seen = True
            run.session_id = _text(obj.get("id")) or p.stem
            run.cwd = _text(obj.get("cwd")) or None
            run.git_branch = _text(obj.get("gitBranch")) or None
        if obj.get("type") != "message" or not isinstance(obj.get("message"), dict):
            continue
        msg = obj["message"]
        content = msg.get("content")
        blocks = content if isinstance(content, list) else [{"type": "text", "text": content}]
        if msg.get("role") == "user" and pwd is None:
            found = _PWD.search(_text(content))
            pwd = found.group(1) if found else None
        if msg.get("role") == "user" and msg.get("visibility") != "llm_only":
            prompt = _CONTEXT.sub("", _text(content)).strip()
            if prompt and not prompt.startswith(("<command-", "<environment_context>", "<user_instructions>")):
                run.prompts.append(prompt)
        model = _text(msg.get("modelId") or msg.get("model")) or default_model
        mid = _text(obj.get("id")) or str(len(issued))
        if msg.get("role") == "assistant":
            observed_models.add(model)
            issued.setdefault(mid, [])
            message_models[mid] = model
            raw_usage = msg.get("usage")
            if isinstance(raw_usage, dict):
                u = _usage(raw_usage)
                previous = message_usage.get(mid)
                if previous is None or u.total >= previous.total:
                    message_usage[mid] = u
            text = _text(content).strip()
            if text:
                run.final_message = text
        for block in blocks:
            if not isinstance(block, dict):
                continue
            call_id = _text(block.get("id"))
            if block.get("type") == "tool_use" and msg.get("role") == "assistant":
                if call_id and call_id in calls:
                    continue
                inp = block.get("input")
                steps = []
                for tool, args in _tools(_text(block.get("name")) or "?", inp if isinstance(inp, dict) else {}):
                    step = Step(len(run.steps) + 1, tool, args, call_id, model, ts)
                    run.steps.append(step)
                    steps.append(step)
                if call_id:
                    calls[call_id] = steps
                issued[mid].extend(steps)
            elif block.get("type") == "tool_result":
                result_id = _text(block.get("tool_use_id"))
                text = _text(block.get("content"))
                failed = block.get("is_error") is True
                exit_code = re.search(r"\[Process exited with code (-?\d+)\]", text)
                results[result_id] = (text[:RESULT_LIMIT], failed or bool(exit_code and int(exit_code[1]) != 0))

    if run.cwd and _is_absolute(run.cwd):
        run.cwd_source = "header"
    else:
        run.cwd, run.cwd_source = _recover_cwd(run, run.cwd or "", pwd, settings, p.parent.name)
    for call_id, steps in calls.items():
        if call_id in results:
            for step in steps:
                step.result_text, step.is_error = results[call_id]
    for mid, steps in issued.items():
        for step in steps:
            step.usage_known = mid in message_usage
    for mid, usage in message_usage.items():
        run.usage.add(usage)
        run.models.setdefault(message_models[mid], Usage()).add(usage)
        for step, share in zip(issued[mid], _split_usage(usage, len(issued[mid])) if issued[mid] else []):
            step.usage = share
    from_settings = False
    raw_own = settings.get("tokenUsage")
    if isinstance(raw_own, dict):
        own = _usage(raw_own)
        remainder = _minus(own, run.usage)
        if remainder.total or not message_usage:
            # A cumulative snapshot cannot identify how much each model used.
            # Never price a mixed-model remainder as the last selected model.
            model = next(iter(observed_models)) if len(observed_models) == 1 else (
                UNATTRIBUTED if observed_models else default_model)
            run.usage.add(remainder)
            run.models.setdefault(model, Usage()).add(remainder)
            from_settings = remainder.total > 0
            pending = [step for mid, steps in issued.items() if mid not in message_usage for step in steps]
            if model not in (UNATTRIBUTED, "unknown") and pending:
                for step, share in zip(pending, _split_usage(remainder, len(pending))):
                    step.usage = share
                    step.usage_estimated = from_settings
                    step.usage_known = from_settings
        children = _child_usage(settings, own, settings_path)
        for model, usage in children.items():
            run.usage.add(usage)
            run.models.setdefault(model, Usage()).add(usage)
            from_settings = from_settings or usage.total > 0
    if not run.usage.total:
        run.usage_source = "missing"
    elif from_settings:
        run.usage_source = "settings"
        run.usage_file = settings_path.name if settings_path else None
    else:
        run.usage_source = "messages"
    return run
