"""Model API list prices in USD per million tokens.

The built-in table is runledger/prices.json (verified official vendor pricing,
checked 2026-10-08). Add ~/.runledger/prices.json or set RUNLEDGER_PRICES to a
JSON file of the same shape to add or replace entries without editing the
package. Unknown models are reported with cost = None instead of a guess.
Cache writes default to the 5-minute rate (1.25x input) and 1-hour cache writes
default to 2x input unless overridden in prices.json. A row outside its
valid_from/valid_until window (or its windows list) is ignored, and the bundled
row stays in effect when a user override is inactive.
"""
from __future__ import annotations

from datetime import date as _date
import json
import math
import os
import re
import sys
from importlib import resources
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from .parser import Run, Usage

PRICES_ENV = "RUNLEDGER_PRICES"
STALE_DAYS = 120
PRICING_HELP = ("Add model prices to ~/.runledger/prices.json (or set RUNLEDGER_PRICES); "
                "see docs/agents.md#costs-across-agents. Bundled prices are re-verified "
                f"every {STALE_DAYS} days and a receipt notes when the checked date is older.")
LONG_CONTEXT_SETTINGS_NOTE = (
    "Token usage came from Droid settings.json totals only; "
    "long-context tier not applied because per-request token counts were unavailable."
)

# claude-opus-4-5, claude-opus-4-20250514, claude-haiku-5-5
_CURRENT_NAME = re.compile(r"claude-([a-z]+)-(\d+)(?:[-.](\d{1,2}))?(?:-|$)")
# legacy order: claude-3-5-haiku-20241022, claude-3-5-haiku-x
_LEGACY_NAME = re.compile(r"claude-(\d+)(?:[-.](\d{1,2}))?-([a-z]+)(?:-|$)")
_MODEL_SUFFIX = re.compile(
    r"(?:(?<!chat)-latest|[-@](?:\d{8}|\d{4}-\d{2}-\d{2})"
    r"|-v\d+(?:[.:]\d+)*|(?<=\d[.-]\d)-0)$"
)
_UNVERIFIED_CLAUDE_VARIANT = re.compile(r"-(?:fast|preview(?:-[a-z0-9.-]+)?|chat-latest)$")
_OTHER_NAME = re.compile(r"([a-z][a-z0-9-]*?)-?(\d.*)\Z")


class _Rate(tuple):
    """A (input, output, cache_read) 3-tuple carrying optional pricing metadata."""
    extra: Dict[str, Any]

    def __new__(cls, inp: float, out: float, cr: float, extra: Optional[Dict[str, Any]] = None):
        obj = super().__new__(cls, (inp, out, cr))
        obj.extra = extra or {}
        return obj


def normalize_model(model: str) -> str:
    """Remove provider/deployment decoration, not meaningful model variants."""
    s = model.strip().lower()
    s = re.sub(r"^custom:", "", s)
    s = s.rsplit("/", 1)[-1]
    s = re.sub(r"^custom:", "", s)
    s = re.sub(r"^(?:[a-z0-9_-]+\.)*anthropic\.", "", s)
    s = re.sub(r"-?\[[^\]]*\]", "", s)
    while True:
        normalized = _MODEL_SUFFIX.sub("", s)
        if normalized == s:
            return s
        s = normalized


def model_key(model: Optional[str]) -> Optional[Tuple[str, str]]:
    """'claude-sonnet-4-5-20250929' -> ('sonnet', '4.5');
    'claude-opus-4-20250514' -> ('opus', '4'); 'gpt-6.1-sol' -> ('gpt', '6.1-sol')."""
    if not model:
        return None
    s = normalize_model(model)
    m = _CURRENT_NAME.search(s)
    if m:
        if _UNVERIFIED_CLAUDE_VARIANT.search(s):
            return None
        family, major, minor = m.groups()
    else:
        m = _LEGACY_NAME.search(s)
        if not m:
            other = _OTHER_NAME.fullmatch(s)
            if not other:
                return None
            family, version = other.groups()
            return family.rstrip("-"), version
        if _UNVERIFIED_CLAUDE_VARIANT.search(s):
            return None
        major, minor, family = m.groups()
    return family, f"{major}.{minor}" if minor else major


def friendly_model(model: Optional[str]) -> str:
    if not model:
        return "unknown"
    normalized = normalize_model(model)
    if not (_CURRENT_NAME.search(normalized) or _LEGACY_NAME.search(normalized)):
        return model
    k = model_key(model)
    if not k:
        return model
    return f"{k[0].capitalize()} {k[1]}"


def _is_price(value: Any) -> bool:
    try:
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and value >= 0 and math.isfinite(value))
    except OverflowError:
        return False


def _parse_extra(entry: Dict[str, Any], source: str, key: str) -> Dict[str, Any]:
    extra: Dict[str, Any] = {}
    for field in ("cache_write_5m", "cache_write_1h"):
        if field in entry and entry[field] is not None:
            if not _is_price(entry[field]):
                raise ValueError(f"{source}: {key!r} {field!r} must be a non-negative number")
            extra[field] = float(entry[field])
    for field in ("valid_from", "valid_until"):
        if field in entry and entry[field] is not None:
            if not isinstance(entry[field], str):
                raise ValueError(f"{source}: {key!r} {field!r} must be a YYYY-MM-DD string")
            extra[field] = entry[field]
    if "long_context" in entry and entry["long_context"] is not None:
        lc = entry["long_context"]
        if not isinstance(lc, dict) or not _is_price(lc.get("threshold_tokens")) or not all(
            _is_price(lc.get(f)) for f in ("input", "output")
        ):
            raise ValueError(f"{source}: {key!r} invalid long_context")
        lc_clean: Dict[str, Any] = {
            "threshold_tokens": int(lc["threshold_tokens"]),
            "input": float(lc["input"]),
            "output": float(lc["output"]),
        }
        for f in ("cache_read", "cache_write_5m", "cache_write_1h"):
            if f in lc and lc[f] is not None:
                if not _is_price(lc[f]):
                    raise ValueError(f"{source}: {key!r} long_context.{f} must be a non-negative number")
                lc_clean[f] = float(lc[f])
        extra["long_context"] = lc_clean
    if "windows" in entry and entry["windows"] is not None:
        wins = entry["windows"]
        if not isinstance(wins, list):
            raise ValueError(f"{source}: {key!r} windows must be a list")
        clean_wins: List[Dict[str, Any]] = []
        for w in wins:
            if not isinstance(w, dict) or not all(
                _is_price(w.get(f)) for f in ("input", "output", "cache_read")
            ):
                raise ValueError(f"{source}: {key!r} window needs input, output, cache_read")
            w_clean = _parse_extra(w, source, key)
            w_clean["input"] = float(w["input"])
            w_clean["output"] = float(w["output"])
            w_clean["cache_read"] = float(w["cache_read"])
            clean_wins.append(w_clean)
        extra["windows"] = clean_wins
    return extra


def _parse_table(data: Any, source: str) -> Dict[Tuple[str, str], Tuple[float, float, float]]:
    if not isinstance(data, dict):
        raise ValueError(f"{source}: top level must be a JSON object")
    table: Dict[Tuple[str, str], Tuple[float, float, float]] = {}
    for key, entry in data.items():
        if key == "checked":  # informational date, not a price
            continue
        family, sep, version = key.partition(":")
        family, version = family.strip().lower(), version.strip().lower()
        if not sep or not family or not version:
            raise ValueError(f'{source}: key {key!r} must be "family:version"')
        if not isinstance(entry, dict) or not all(
                _is_price(entry.get(f)) for f in ("input", "output", "cache_read")):
            raise ValueError(f'{source}: {key!r} needs finite non-negative numbers '
                             f'"input", "output" and "cache_read"')
        extra = _parse_extra(entry, source, key)
        table[(family, version)] = _Rate(
            float(entry["input"]), float(entry["output"]), float(entry["cache_read"]), extra
        )
    return table


_WARNED: set[Tuple[str, str]] = set()
_DEFAULT_TEXT = resources.files(__package__).joinpath("prices.json").read_text(encoding="utf-8")
_DEFAULT_RAW = json.loads(_DEFAULT_TEXT)
CHECKED: str = str(_DEFAULT_RAW.get("checked", "2026-10-08"))


def _today() -> str:
    """Today as YYYY-MM-DD. Validity windows are evaluated against it; tests patch it."""
    return _date.today().isoformat()


def _in_window(extra: Dict[str, Any], today: str) -> bool:
    """True when a table row is alive on `today`: no window at all, inside its own
    valid_from/valid_until, or inside one of its `windows` entries."""
    if _inside(extra, today):
        return True
    return any(_inside(w, today) for w in extra.get("windows", []))


def _inside(extra: Dict[str, Any], today: str) -> bool:
    valid_from = extra.get("valid_from")
    valid_until = extra.get("valid_until")
    return (not valid_from or today >= valid_from) and (not valid_until or today <= valid_until)


def load_prices(override: Optional[Union[str, os.PathLike]] = None
                ) -> Dict[Tuple[str, str], Tuple[float, float, float]]:
    """Built-in prices merged with a JSON override file. The override is `override`
    if given, otherwise $RUNLEDGER_PRICES or ~/.runledger/prices.json. Invalid
    overrides warn once per process and are ignored in full. A missing default user file is normal.
    A user row outside its validity window is skipped, so the bundled row stays in effect."""
    prices = _parse_table(_DEFAULT_RAW, "prices.json")
    configured = override if override is not None else os.environ.get(PRICES_ENV)
    path = Path(configured or "~/.runledger/prices.json")
    try:
        path = path.expanduser()
        text = path.read_text(encoding="utf-8")
        user = _parse_table(json.loads(text), str(path))
        today = _today()
        prices.update({k: v for k, v in user.items()
                       if _in_window(getattr(v, "extra", {}), today)})
    except (OSError, ValueError, RuntimeError) as exc:
        if configured or not isinstance(exc, FileNotFoundError):
            detail = " ".join(str(exc).split())
            warn_key = (str(path), detail)
            if warn_key not in _WARNED:
                _WARNED.add(warn_key)
                print(f"warning: ignoring prices file {str(path)!r}: {detail}", file=sys.stderr)
    return prices


# (family, version) -> (input, output, cache_read), loaded once at import
PRICES: Dict[Tuple[str, str], Tuple[float, float, float]] = load_prices()


def is_stale(today: Optional[Union[str, _date]] = None) -> bool:
    """Return True when today is > STALE_DAYS (120) days past CHECKED."""
    try:
        chk = _date.fromisoformat(str(CHECKED)[:10])
        if isinstance(today, str):
            cur = _date.fromisoformat(today[:10])
        elif isinstance(today, _date):
            cur = today
        else:
            cur = _date.fromisoformat(_today())
        return (cur - chk).days > STALE_DAYS
    except ValueError:
        return False


def stale_note() -> str:
    return (
        f"Bundled model prices were last checked on {CHECKED} (> {STALE_DAYS} days ago); "
        "verify current rates or override via ~/.runledger/prices.json."
    )


def _rate_for(model: Optional[str], date: Optional[str] = None) -> Optional[_Rate]:
    k = model_key(model)
    if not k:
        return None
    entry = PRICES.get(k)
    if not entry:
        return None
    extra = getattr(entry, "extra", {})
    if not isinstance(entry, _Rate):
        entry = _Rate(entry[0], entry[1], entry[2], extra)
    d = str(date)[:10] if date else _today()
    if _inside(extra, d):
        return entry
    for w in extra.get("windows", []):
        if _inside(w, d):
            return _Rate(w["input"], w["output"], w["cache_read"], w)
    return None


def price_for(model: Optional[str], date: Optional[str] = None) -> Optional[Tuple[float, float, float]]:
    return _rate_for(model, date=date)


def has_long_context(model: Optional[str]) -> bool:
    r = _rate_for(model)
    return bool(r and r.extra.get("long_context"))


def _single_cost(usage: Usage, rate: _Rate, allow_long_context: bool = True) -> float:
    inp, out, cache_read = rate
    extra = rate.extra
    cw_1h = max(0, getattr(usage, "cache_write_1h_tokens", 0))
    cw_total = max(usage.cache_write_tokens, cw_1h)
    cw_5m = max(0, usage.cache_write_tokens - cw_1h)

    cw5_rate = float(extra.get("cache_write_5m", inp * 1.25))
    cw1h_rate = float(extra.get("cache_write_1h", inp * 2.0))

    lc = (
        extra.get("long_context")
        if allow_long_context and not getattr(usage, "_cumulative_only", False)
        else None
    )
    if lc:
        req_prompt = getattr(usage, "_request_prompt_tokens", None)
        if req_prompt is None:
            req_prompt = usage.input_tokens + usage.cache_read_tokens + cw_total
        if req_prompt > lc["threshold_tokens"]:
            inp = float(lc["input"])
            out = float(lc["output"])
            cache_read = float(lc.get("cache_read", cache_read))
            cw5_rate = float(lc.get("cache_write_5m", inp * 1.25))
            cw1h_rate = float(lc.get("cache_write_1h", inp * 2.0))

    return (
        usage.input_tokens * inp
        + usage.output_tokens * out
        + cw_5m * cw5_rate
        + cw_1h * cw1h_rate
        + usage.cache_read_tokens * cache_read
    ) / 1_000_000


def _request_cost(usage: Usage, rate: _Rate) -> float:
    """One request's cost: tiered unless the usage is a cumulative estimate."""
    return _single_cost(usage, rate, allow_long_context=not getattr(usage, "_cumulative_only", False))


def _uncovered(usage: Usage, requests: List[Usage]) -> Usage:
    """The part of an aggregate that no known request covers. Its per-request
    boundaries are unknown, so it is priced as a cumulative estimate (base tier)."""
    def rest(field: str) -> int:
        return max(0, getattr(usage, field) - sum(getattr(r, field) for r in requests))

    remainder = Usage(
        input_tokens=rest("input_tokens"),
        output_tokens=rest("output_tokens"),
        cache_write_tokens=rest("cache_write_tokens"),
        cache_read_tokens=rest("cache_read_tokens"),
        cache_write_1h_tokens=rest("cache_write_1h_tokens"),
    )
    remainder._cumulative_only = True
    return remainder


def cost_of(
    usage: Usage,
    model: Optional[str],
    date: Optional[str] = None,
    allow_long_context: bool = True,
) -> Optional[float]:
    rate = _rate_for(model, date=date)
    if not rate:
        return None
    reqs = getattr(usage, "_requests", None)
    if reqs and allow_long_context:
        covered = sum(r.total for r in reqs)
        if covered == usage.total:
            return sum(_request_cost(r, rate) for r in reqs)
        if covered < usage.total and any(not r._cumulative_only for r in reqs):
            known = sum(_request_cost(r, rate) for r in reqs)
            return known + _single_cost(_uncovered(usage, reqs), rate, allow_long_context=False)
    return _single_cost(usage, rate, allow_long_context=allow_long_context)


UNATTRIBUTED = "unattributed"  # session-total tokens that no single model can be credited with
UNATTRIBUTED_NOTE = ("Some tokens come from a session total that cannot be credited to one model "
                     "(several models or sub-agents), so they have no price.")


def unknown_models(run: Run) -> List[str]:
    """Model ids used in the run that have no price, so their cost is unknown.
    The unattributed bucket is not a model: adding a price cannot fix it."""
    return [model for model in run.models if model != UNATTRIBUTED and price_for(model) is None]


def unpriced(run: Run) -> List[str]:
    """Buckets with tokens but no price, including unattributed tokens."""
    return [m for m, u in run.models.items() if u.total > 0 and price_for(m) is None]


def partial_note(run: Run) -> str:
    """Why a cost is a lower bound, e.g. "1 model unpriced"; empty when the cost is complete."""
    if not run.cost_partial:
        return ""
    missing = unpriced(run)
    models = [m for m in missing if m != UNATTRIBUTED]
    parts = [f"{len(models)} model{'s' if len(models) != 1 else ''} unpriced"] if models else []
    if UNATTRIBUTED in missing:
        parts.append("unattributed tokens unpriced")
    return ", ".join(parts)


def apply_costs(run: Run) -> None:
    """Price every step and model bucket. Tiering follows each request's own usage;
    buckets that are only cumulative estimates (Droid settings totals, aggregate
    native totals) keep the base tier and are noted. An agent that recorded no usage
    leaves the run cost unknown, never a priced zero."""
    for s in run.steps:
        s.cost = cost_of(s.usage, s.model) if s.usage_known else None
    if run.usage_source == "missing":
        run.cost = None
        run.cost_partial = False
        return
    total = 0.0
    known = False
    for model, u in run.models.items():
        if run.usage_source == "settings" and u.total > 0 and has_long_context(model):
            if LONG_CONTEXT_SETTINGS_NOTE not in run.notes:
                run.notes.append(LONG_CONTEXT_SETTINGS_NOTE)
        c = cost_of(u, model)
        if c is not None:
            total += c
            known = True
    run.cost = total if known else None
    run.cost_partial = known and bool(unpriced(run))
