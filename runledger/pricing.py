"""Claude API list prices in USD per million tokens.

The built-in table is runledger/prices.json (Anthropic pricing page, checked
2026-10-08). Add ~/.runledger/prices.json or set RUNLEDGER_PRICES to a JSON file
of the same shape to add or replace entries without editing the package.
Unknown models are reported with cost = None instead of a guess.
Cache writes are priced at the 5-minute rate
(1.25x input).
"""
from __future__ import annotations

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
PRICING_HELP = ("Add model prices to ~/.runledger/prices.json (or set RUNLEDGER_PRICES); "
                "see docs/agents.md#costs-across-agents.")

# claude-opus-4-5, claude-opus-4-20250514, claude-haiku-5-5
_CURRENT_NAME = re.compile(r"claude-([a-z]+)-(\d+)(?:[-.](\d{1,2}))?(?:-|$)")
# legacy order: claude-3-5-haiku-20241022, claude-3-5-haiku-x
_LEGACY_NAME = re.compile(r"claude-(\d+)(?:[-.](\d{1,2}))?-([a-z]+)(?:-|$)")
_MODEL_SUFFIX = re.compile(r"(?:-latest|[-@](?:\d{8}|\d{4}-\d{2}-\d{2})"
                           r"|-v\d+(?:[.:]\d+)*)$")
_OTHER_NAME = re.compile(r"([a-z][a-z0-9-]*?)-?(\d.*)\Z")


def normalize_model(model: str) -> str:
    """Remove provider/deployment decoration, not meaningful model variants."""
    model = model.strip().lower().rsplit("/", 1)[-1]
    while True:
        normalized = _MODEL_SUFFIX.sub("", model)
        if normalized == model:
            return model
        model = normalized


def model_key(model: Optional[str]) -> Optional[Tuple[str, str]]:
    """'claude-sonnet-4-5-20250929' -> ('sonnet', '4.5');
    'claude-opus-4-20250514' -> ('opus', '4'); 'gpt-6.1-sol' -> ('gpt', '6.1-sol')."""
    if not model:
        return None
    s = normalize_model(model)
    m = _CURRENT_NAME.search(s)
    if m:
        family, major, minor = m.groups()
    else:
        m = _LEGACY_NAME.search(s)
        if not m:
            other = _OTHER_NAME.fullmatch(s)
            if not other:
                return None
            family, version = other.groups()
            return family.rstrip("-"), version
        major, minor, family = m.groups()
    return family, f"{major}.{minor}" if minor else major


def friendly_model(model: Optional[str]) -> str:
    if not model:
        return "unknown"
    normalized = normalize_model(model)
    if not (_CURRENT_NAME.search(normalized) or _LEGACY_NAME.search(normalized)):
        return model
    k = model_key(model)
    return f"{k[0].capitalize()} {k[1]}"


def _is_price(value: Any) -> bool:
    try:
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and value >= 0 and math.isfinite(value))
    except OverflowError:
        return False


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
        table[(family, version)] = (float(entry["input"]), float(entry["output"]),
                                    float(entry["cache_read"]))
    return table


def load_prices(override: Optional[Union[str, os.PathLike]] = None
                ) -> Dict[Tuple[str, str], Tuple[float, float, float]]:
    """Built-in prices merged with a JSON override file. The override is `override`
    if given, otherwise $RUNLEDGER_PRICES or ~/.runledger/prices.json. Invalid
    overrides warn and are ignored in full. A missing default user file is normal."""
    default_text = resources.files(__package__).joinpath("prices.json").read_text(encoding="utf-8")
    prices = _parse_table(json.loads(default_text), "prices.json")
    configured = override if override is not None else os.environ.get(PRICES_ENV)
    path = Path(configured or "~/.runledger/prices.json")
    try:
        path = path.expanduser()
        text = path.read_text(encoding="utf-8")
        prices.update(_parse_table(json.loads(text), str(path)))
    except (OSError, ValueError, RuntimeError) as exc:
        if configured or not isinstance(exc, FileNotFoundError):
            detail = " ".join(str(exc).split())
            print(f"warning: ignoring prices file {str(path)!r}: {detail}", file=sys.stderr)
    return prices


# (family, version) -> (input, output, cache_read), loaded once at import
PRICES: Dict[Tuple[str, str], Tuple[float, float, float]] = load_prices()


def price_for(model: Optional[str]) -> Optional[Tuple[float, float, float]]:
    k = model_key(model)
    return PRICES.get(k) if k else None


def cost_of(usage: Usage, model: Optional[str]) -> Optional[float]:
    p = price_for(model)
    if not p:
        return None
    inp, out, cache_read = p
    return (usage.input_tokens * inp
            + usage.output_tokens * out
            + usage.cache_write_tokens * inp * 1.25
            + usage.cache_read_tokens * cache_read) / 1_000_000


def unknown_models(run: Run) -> List[str]:
    """Model ids used in the run that have no price, so their cost is unknown."""
    return [model for model in run.models if price_for(model) is None]


def apply_costs(run: Run) -> None:
    for s in run.steps:
        s.cost = cost_of(s.usage, s.model) if s.usage_known else None
    total = 0.0
    known = False
    for model, u in run.models.items():
        c = cost_of(u, model)
        if c is not None:
            total += c
            known = True
    run.cost = total if known else None
