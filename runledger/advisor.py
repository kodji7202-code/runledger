"""Cost recommendations for one run: where it spent more than it needed to.

Every figure is an estimate. A step's cost comes from the list prices in pricing.py
applied to the tokens that step was charged for. A saving is the difference between
what the step cost and what the same tokens would cost on a cheaper model or with
prompt caching. Steps whose model has no price contribute nothing, so every
estimate is a lower bound; unknown_pricing reports the missing prices themselves.

A recommendation is kept only when it is material: its estimated saving is at least
$0.01, or at least 20% of the run's estimated cost. A run that costs less than one
cent gets no priced recommendation. unknown_pricing has no estimate and is always
reported when a model with tokens has no price.

recommend(run) returns a list shaped like this (see docs/analysis.md):
  [{"kind": str, "title": str, "detail": str, "est_savings_usd": float | None,
    "steps": [int]}]
sorted by est_savings_usd, largest first; entries without an estimate come last.
Kinds: model_downgrade, cache_misses, retry_loop, large_reads, unknown_pricing.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional

from .parser import Run, Step
from .pricing import cost_of, friendly_model, model_key, price_for, unknown_models

HAIKU = "claude-haiku-5-5"
MIN_SAVING_USD = 0.01
MIN_SAVING_SHARE = 0.20          # or 20% of the run's estimated cost
LONG_SESSION_STEPS = 20
CACHE_HIT_FLOOR = 0.30           # cache reads below this share of input-side tokens are misses
CACHEABLE_TOKENS = 50_000        # input-side tokens a session needs before caching is worth raising
REPEATED_CONTEXT_SHARE = 0.5     # assumed share of uncached input that repeats between turns
READ_REPEAT_MIN = 3
LARGE_RESULT_CHARS = 4000        # the parser keeps 4000 characters of each tool result
SHELL_TOOLS = ("Bash", "PowerShell")
SEARCH_TOOLS = ("Read", "Search", "Grep", "Glob", "LS")
EXPENSIVE_FAMILIES = ("opus", "sonnet")

_READ_ONLY_COMMANDS = {
    "ls", "dir", "cat", "head", "tail", "pwd", "echo", "printf", "grep", "rg", "find", "wc",
    "which", "where", "type", "tree", "get-childitem", "get-content", "select-string", "gci",
    "gc", "sls", "get-location",
}
_READ_ONLY_GIT = {"status", "log", "diff", "show", "branch", "rev-parse", "ls-files"}
_DEVNULL = re.compile(r"\d*>&\d|\d*>\s*(?:/dev/null|nul)\b", re.I)
_UNSAFE_SHELL = re.compile(r"[;&|\n<>`]|\$\(|\s-(?:delete|exec)\b|\btee\b")


def _usd(value: float) -> str:
    return f"${value:.2f}" if value >= 0.01 else f"${value:.4f}"


def _command(step: Step) -> str:
    return " ".join(str(step.input.get("command") or "").split())


def _short(text: str, limit: int = 80) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _steps_text(indices: List[int]) -> str:
    return ", ".join(str(i) for i in indices)


def estimated_cost(run: Run) -> Optional[float]:
    """Estimated run cost from the list prices. None when no model has a price."""
    total, known = 0.0, False
    for model, usage in run.models.items():
        c = cost_of(usage, model)
        if c is not None:
            total += c
            known = True
    return total if known else None


def _material(saving: float, run_cost: Optional[float]) -> bool:
    """A saving is material when it is at least $0.01, or at least 20% of the run's cost.
    A run that costs less than one cent gets no priced recommendation at all."""
    if saving <= 0 or run_cost is None or run_cost < MIN_SAVING_USD:
        return False
    return saving >= MIN_SAVING_USD or saving >= MIN_SAVING_SHARE * run_cost


def _rec(kind: str, title: str, detail: str, saving: Optional[float], steps: List[int]) -> Dict[str, Any]:
    return {
        "kind": kind,
        "title": title,
        "detail": detail,
        "est_savings_usd": None if saving is None else round(saving, 4),
        "steps": steps,
    }


# ---------------------------------------------------------------- model downgrade

def _is_read_only(step: Step) -> bool:
    """Reads, searches, listings and simple read-only shell commands."""
    if step.tool in SEARCH_TOOLS:
        return True
    if step.tool not in SHELL_TOOLS:
        return False
    cmd = _command(step)
    if not cmd:
        return False
    cleaned = _DEVNULL.sub(" ", cmd)  # 2>&1 and 2>/dev/null do not write files
    if _UNSAFE_SHELL.search(cleaned):
        return False
    words = cleaned.split()
    head = words[0].lower()
    if head == "git":
        return len(words) > 1 and words[1].lower() in _READ_ONLY_GIT
    return head in _READ_ONLY_COMMANDS


def _is_expensive(model: Optional[str]) -> bool:
    key = model_key(model)
    return key is not None and key[0] in EXPENSIVE_FAMILIES


def _model_downgrade(run: Run, run_cost: Optional[float]) -> Optional[Dict[str, Any]]:
    picked = [s for s in run.steps if _is_expensive(s.model) and _is_read_only(s)]
    if not picked:
        return None
    actual = cheap = 0.0
    for s in picked:
        now, alt = cost_of(s.usage, s.model), cost_of(s.usage, HAIKU)
        if now is None or alt is None:
            continue  # unpriced: left out, so the estimate stays a lower bound
        actual += now
        cheap += alt
    saving = actual - cheap
    if not _material(saving, run_cost):
        return None
    models = ", ".join(sorted({friendly_model(s.model) for s in picked}))
    detail = (f"{len(picked)} of {len(run.steps)} steps were reads, searches, listings or simple "
              f"shell commands, and ran on {models}. At {friendly_model(HAIKU)} list prices the same "
              f"tokens would cost about {_usd(cheap)} instead of {_usd(actual)}. "
              f"Estimated saving: {_usd(saving)}.")
    return _rec("model_downgrade", f"Run read-only steps on {friendly_model(HAIKU)}", detail, saving,
                [s.index for s in picked])


# ---------------------------------------------------------------- prompt cache

def _cache_misses(run: Run, run_cost: Optional[float]) -> Optional[Dict[str, Any]]:
    if len(run.steps) < LONG_SESSION_STEPS:
        return None
    fresh = cached = written = 0
    for usage in run.models.values():
        fresh += usage.input_tokens
        cached += usage.cache_read_tokens
        written += usage.cache_write_tokens
    side = fresh + written + cached
    if side < CACHEABLE_TOKENS or cached / side >= CACHE_HIT_FLOOR:
        return None
    saving = 0.0
    for model, usage in run.models.items():
        price = price_for(model)
        if price:
            input_price, _, cache_read_price = price
            saving += (REPEATED_CONTEXT_SHARE * usage.input_tokens
                       * (input_price - cache_read_price) / 1_000_000)
    if not _material(saving, run_cost):
        return None
    misses = [s.index for s in run.steps if s.usage.input_tokens > 0 and s.usage.cache_read_tokens == 0]
    detail = (f"Only {cached / side:.0%} of input tokens across {len(run.steps)} steps were read from "
              f"the prompt cache. Context that repeats between turns (instructions, earlier file reads) "
              f"is billed at full input price each time. Keeping the stable part of the context unchanged "
              f"between turns, or starting a fresh session for each new task, lets the cache do more of "
              f"the work. Estimated saving: {_usd(saving)}, assuming half of the uncached input repeats.")
    return _rec("cache_misses", "Little prompt caching in a long session", detail, saving, misses)


# ---------------------------------------------------------------- retry loops

def _retry_loop(run: Run, run_cost: Optional[float]) -> Optional[Dict[str, Any]]:
    groups: Dict[str, List[Step]] = {}
    for s in run.steps:
        if s.tool in SHELL_TOOLS and s.is_error:
            key = _command(s)
            if key:
                groups.setdefault(key, []).append(s)
    repeated = {k: v for k, v in groups.items() if len(v) >= 2}
    if not repeated:
        return None
    wasted = 0.0
    steps: List[int] = []
    for group in repeated.values():
        steps += [s.index for s in group]
        for s in group[1:]:  # the first failure is the one that taught something
            c = cost_of(s.usage, s.model)
            wasted += c or 0.0
    if not _material(wasted, run_cost):
        return None
    worst_cmd, worst = max(repeated.items(), key=lambda kv: len(kv[1]))
    detail = (f"{len(repeated)} shell command(s) failed more than once. The most repeated "
              f"(“{_short(worst_cmd)}”) failed {len(worst)} times. Retries after the first failure cost "
              f"about {_usd(wasted)} (estimate). Read the error once and change the approach instead of "
              f"running the same command again.")
    return _rec("retry_loop", "Repeated failing commands", detail, wasted, sorted(set(steps)))


# ---------------------------------------------------------------- large reads

def _large_reads(run: Run, run_cost: Optional[float]) -> Optional[Dict[str, Any]]:
    reads = [s for s in run.steps if s.tool == "Read"]
    by_file: Dict[str, List[Step]] = {}
    for s in reads:
        path = str(s.input.get("file_path") or "")
        if path:
            by_file.setdefault(path, []).append(s)
    repeated_files = [g for g in by_file.values() if len(g) >= READ_REPEAT_MIN]
    redundant = [s for g in repeated_files for s in g[2:]]  # reads after the second of each file
    big = [s for s in reads if len(s.result_text or "") >= LARGE_RESULT_CHARS]
    if not redundant and not big:
        return None
    total = 0.0
    for s in redundant:
        total += cost_of(s.usage, s.model) or 0.0
    steps_total = len(run.steps)
    for s in big:
        price = price_for(s.model)
        if price:
            # the result stays in context for every later step, mostly as cached input
            approx_tokens = len(s.result_text) / 4
            total += approx_tokens * max(0, steps_total - s.index) * price[2] / 1_000_000
    if not _material(total, run_cost):
        return None
    detail = (f"{len(repeated_files)} file(s) were read {READ_REPEAT_MIN} or more times, and {len(big)} "
              f"Read result(s) were {LARGE_RESULT_CHARS:,}+ characters. Reading only the lines needed "
              f"(offset and limit) keeps the context that every later step carries. "
              f"Estimated saving: {_usd(total)}.")
    steps = sorted({s.index for s in redundant} | {s.index for s in big})
    return _rec("large_reads", "Large or repeated file reads", detail, total, steps)


# ---------------------------------------------------------------- unknown pricing

def _unknown_pricing(run: Run, run_cost: Optional[float]) -> Optional[Dict[str, Any]]:
    models = sorted(m for m in unknown_models(run) if run.models[m].total > 0)
    if not models:
        return None
    steps = [s.index for s in run.steps if (s.model or "unknown") in models]
    detail = (f"No list price is known for {', '.join(models)}, so their cost is missing from the "
              f"totals. Add prices with RUNLEDGER_PRICES, a JSON file shaped like prices.json.")
    return _rec("unknown_pricing", f"No price for {len(models)} model(s)", detail, None, steps)


_BUILDERS: List[Callable[[Run, Optional[float]], Optional[Dict[str, Any]]]] = [
    _model_downgrade, _cache_misses, _retry_loop, _large_reads, _unknown_pricing,
]


def recommend(run: Run) -> List[Dict[str, Any]]:
    run_cost = estimated_cost(run)
    recs = [rec for build in _BUILDERS if (rec := build(run, run_cost)) is not None]
    recs.sort(key=lambda r: (r["est_savings_usd"] is None, -(r["est_savings_usd"] or 0.0)))
    return recs
