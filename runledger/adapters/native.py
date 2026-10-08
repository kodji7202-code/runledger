"""RunLedger's open session format, for agents that have no adapter of their own.

Any agent can write one of these files and get a receipt, a risk score and a
push, with no RunLedger code. The full specification is docs/format.md.

Two layouts with the same fields:
  <name>.runledger.json    one JSON object: the header fields plus "prompts"
                           and "steps" arrays
  <name>.runledger.jsonl   line 1 is the header, every later line is a step,
                           and an optional last line {"type": "end", ...}

Files are discovered in <project>/.runledger/runs/ (directly inside it).
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..parser import Run, Step, Usage

NAME = "native"
LABEL = "RunLedger format"
FORMAT_VERSION = 1
DEFAULT_AGENT = "native"
RUNS_DIR = (".runledger", "runs")
SUFFIXES = (".runledger.json", ".runledger.jsonl")
RESULT_LIMIT = 4000  # same cap as the Claude Code adapter
_USAGE_FIELDS = ("input_tokens", "output_tokens", "cache_write_tokens", "cache_read_tokens")

# (location for error messages, parsed object)
_Record = Tuple[str, Dict[str, Any]]


def _suffix(path: Path) -> Optional[str]:
    name = path.name.lower()
    for suffix in SUFFIXES:
        if name.endswith(suffix):
            return suffix
    return None


def _base_name(path: Path) -> str:
    suffix = _suffix(path)
    if suffix:
        return path.name[: len(path.name) - len(suffix)]
    return path.stem


def detect(path) -> bool:
    """True for *.runledger.json / *.runledger.jsonl, and for any .json or .jsonl
    whose first JSON object has a "runledger_format" key. Never raises."""
    p = Path(path)
    if _suffix(p):
        return True
    if p.suffix.lower() not in (".json", ".jsonl"):
        return False
    try:
        with open(p, "r", encoding="utf-8-sig") as fh:
            if p.suffix.lower() == ".jsonl":
                text = next((line for line in fh if line.strip()), "")
            else:
                text = fh.read()
        obj = json.loads(text)
    except (OSError, ValueError):
        return False
    return isinstance(obj, dict) and "runledger_format" in obj


def parse(path) -> Run:
    """Read and validate a native session file. Raises ValueError with the file
    name and a line number (JSONL) or step number (JSON) for bad input."""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{p}: not UTF-8 text ({exc.reason})") from None
    if _is_jsonl(p, text):
        header, steps, end = _read_jsonl(p, text)
    else:
        header, steps, end = _read_json(p, text)
    return _to_run(p, header, steps, end)


def find_sessions(project: Optional[str] = None) -> List[Path]:
    if not project:  # native files live inside a project; no project, no sessions
        return []
    runs = Path(project).joinpath(*RUNS_DIR)
    if not runs.is_dir():
        return []
    files = [f for f in runs.iterdir() if f.is_file() and _suffix(f)]
    return sorted(files, key=lambda f: f.stat().st_mtime, reverse=True)


# ---------------------------------------------------------------- reading

def _is_jsonl(path: Path, text: str) -> bool:
    suffix = _suffix(path)
    if suffix == ".runledger.jsonl":
        return True
    if suffix == ".runledger.json":
        return False
    try:
        json.loads(text)
    except ValueError:
        return True  # several JSON values, one per line
    return False


def _read_json(path: Path, text: str) -> Tuple[_Record, List[_Record], Optional[_Record]]:
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        hint = " (one JSON object per line needs the .runledger.jsonl name)" if exc.msg == "Extra data" else ""
        raise ValueError(f"{path}: line {exc.lineno}, column {exc.colno}: invalid JSON ({exc.msg}){hint}") from None
    if not isinstance(doc, dict):
        raise ValueError(f"{path}: the top level must be a JSON object")
    raw_steps = doc.get("steps")
    if raw_steps is None:
        raw_steps = []
    if not isinstance(raw_steps, list):
        raise ValueError(f"{path}: 'steps' must be an array")
    steps = [(f"{path}: step {i}", obj) for i, obj in enumerate(raw_steps, start=1)]
    return (str(path), doc), steps, None


def _read_jsonl(path: Path, text: str) -> Tuple[_Record, List[_Record], Optional[_Record]]:
    header: Optional[_Record] = None
    end: Optional[_Record] = None
    steps: List[_Record] = []
    # split on "\n" only: str.splitlines also breaks on U+2028, which JSON strings may contain
    for lineno, raw in enumerate(text.split("\n"), start=1):
        line = raw.strip()
        if not line:
            continue
        loc = f"{path}: line {lineno}"
        if end is not None:
            raise ValueError(f"{loc}: nothing may follow the end record")
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{loc}: invalid JSON ({exc.msg})") from None
        if not isinstance(obj, dict):
            raise ValueError(f"{loc}: each line must be a JSON object")
        if header is None:
            if "runledger_format" not in obj:
                raise ValueError(f"{loc}: the first line must be the header object (it needs runledger_format)")
            header = (loc, obj)
            continue
        kind = obj.get("type", "step")
        if kind == "end":
            end = (loc, obj)
        elif kind == "step":
            steps.append((loc, obj))
        else:
            raise ValueError(f"{loc}: unknown record type {kind!r} (use \"step\" or \"end\")")
    if header is None:
        raise ValueError(f"{path}: empty file (the first line must be the header object)")
    return header, steps, end


# ---------------------------------------------------------------- validation

def _text(loc: str, key: str, value: Any) -> Optional[str]:
    if value is None or isinstance(value, str):
        return value
    raise ValueError(f"{loc}: '{key}' must be a string or null")


def _usage(loc: str, key: str, raw: Any) -> Usage:
    if raw is None:
        return Usage()
    if not isinstance(raw, dict):
        raise ValueError(f"{loc}: '{key}' must be an object")
    values = {}
    for field in _USAGE_FIELDS:
        value = raw.get(field)
        if value is None:
            value = 0
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{loc}: '{key}.{field}' must be a non-negative whole number")
        values[field] = value
    return Usage(**values)


def _models(loc: str, raw: Any) -> Dict[str, Usage]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"{loc}: 'models' must be an object keyed by model id")
    out: Dict[str, Usage] = {}
    for model, usage in raw.items():
        if not model.strip():
            raise ValueError(f"{loc}: model ids must not be empty")
        out[model] = _usage(loc, f"models.{model}", usage)
    return out


def _reported_cost(loc: str, raw: Any) -> Optional[float]:
    if raw is None:
        return None
    if (isinstance(raw, bool) or not isinstance(raw, (int, float))
            or not math.isfinite(raw) or raw < 0):
        raise ValueError(f"{loc}: 'reported_cost' must be a non-negative number or null")
    return float(raw)


def _step(loc: str, index: int, obj: Any, default_model: Optional[str]) -> Step:
    if not isinstance(obj, dict):
        raise ValueError(f"{loc}: a step must be a JSON object")
    tool = obj.get("tool")
    if not isinstance(tool, str) or not tool.strip():
        raise ValueError(f"{loc}: 'tool' must be a non-empty string")
    tool_input = obj.get("input")
    if tool_input is None:
        tool_input = {}
    if not isinstance(tool_input, dict):
        raise ValueError(f"{loc}: 'input' must be an object")
    is_error = obj.get("is_error")
    if is_error is None:
        is_error = False
    if not isinstance(is_error, bool):
        raise ValueError(f"{loc}: 'is_error' must be true or false")
    model = _text(loc, "model", obj.get("model")) or default_model
    result = _text(loc, "result_text", obj.get("result_text")) or ""
    return Step(
        index=index,
        tool=tool,
        input=tool_input,
        tool_use_id="",
        model=model,
        timestamp=_text(loc, "timestamp", obj.get("timestamp")),
        usage=_usage(loc, "usage", obj.get("usage")),
        result_text=result[:RESULT_LIMIT],
        is_error=is_error,
    )


def _to_run(path: Path, header: _Record, steps: List[_Record], end: Optional[_Record]) -> Run:
    hloc, hobj = header

    def pick(key: str) -> Tuple[str, Any]:
        """A field comes from the end record when it repeats it there, else the header."""
        if end is not None and key in end[1]:
            return end[0], end[1][key]
        return hloc, hobj.get(key)

    fmt = hobj.get("runledger_format")
    if isinstance(fmt, bool) or not isinstance(fmt, int) or fmt != FORMAT_VERSION:
        raise ValueError(f"{hloc}: unsupported runledger_format {fmt!r} "
                         f"(this RunLedger reads format {FORMAT_VERSION})")

    def text(key: str) -> Optional[str]:
        loc, value = pick(key)
        return _text(loc, key, value)

    agent = (text("agent") or "").strip() or DEFAULT_AGENT
    session_id = text("session_id") or _base_name(path)

    loc, raw_prompts = pick("prompts")
    if raw_prompts is None:
        raw_prompts = []
    if not (isinstance(raw_prompts, list) and all(isinstance(p, str) for p in raw_prompts)):
        raise ValueError(f"{loc}: 'prompts' must be an array of strings")

    loc, raw_models = pick("models")
    models = _models(loc, raw_models)
    loc, raw_cost = pick("reported_cost")
    reported = _reported_cost(loc, raw_cost)

    # One model in the run: steps without their own "model" are priced with it.
    default_model = next(iter(models)) if len(models) == 1 else None
    run_steps = [_step(sloc, i, obj, default_model) for i, (sloc, obj) in enumerate(steps, start=1)]

    total = Usage()
    for usage in (models.values() if models else (s.usage for s in run_steps)):
        total.add(usage)

    return Run(
        session_id=session_id,
        path=str(path),
        cwd=text("cwd"),
        git_branch=text("git_branch"),
        started=text("started"),
        ended=text("ended"),
        prompts=list(raw_prompts),
        steps=run_steps,
        final_message=text("final_message") or "",
        usage=total,
        models=models,
        agent=agent,
        reported_cost=reported,
    )
