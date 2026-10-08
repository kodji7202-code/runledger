"""Claude API list prices in USD per million tokens.

The built-in table is runledger/prices.json (Anthropic pricing page, checked
2026-10-08). Set RUNLEDGER_PRICES to a JSON file of the same shape to add or
replace entries without editing the package. Unknown models are reported with
cost = None instead of a guess. Cache writes are priced at the 5-minute rate
(1.25x input).
"""
from __future__ import annotations

import json
import os
import re
from importlib import resources
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from .parser import Run, Usage

PRICES_ENV = "RUNLEDGER_PRICES"

# claude-opus-4-5, claude-opus-4-20250514, claude-haiku-5-5
_CURRENT_NAME = re.compile(r"claude-([a-z]+)-(\d+)(?:-(\d{1,2}))?(?:-|$)")
# legacy order: claude-3-5-haiku-20241022, claude-3-5-haiku-x
_LEGACY_NAME = re.compile(r"claude-(\d+)(?:-(\d{1,2}))?-([a-z]+)(?:-|$)")


def model_key(model: Optional[str]) -> Optional[Tuple[str, str]]:
    """'claude-sonnet-4-5-20250929' -> ('sonnet', '4.5');
    'claude-opus-4-20250514' -> ('opus', '4'); 'claude-3-5-haiku-x' -> ('haiku', '3.5')."""
    if not model:
        return None
    s = model.lower()
    m = _CURRENT_NAME.search(s)
    if m:
        family, major, minor = m.groups()
    else:
        m = _LEGACY_NAME.search(s)
        if not m:
            return None
        major, minor, family = m.groups()
    return family, f"{major}.{minor}" if minor else major


def friendly_model(model: Optional[str]) -> str:
    k = model_key(model)
    if not k:
        return model or "unknown"
    return f"{k[0].capitalize()} {k[1]}"


def _is_price(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0


def _parse_table(data: Any, source: str) -> Dict[Tuple[str, str], Tuple[float, float, float]]:
    if not isinstance(data, dict):
        raise ValueError(f"{source}: top level must be a JSON object")
    table: Dict[Tuple[str, str], Tuple[float, float, float]] = {}
    for key, entry in data.items():
        if key == "checked":  # informational date, not a price
            continue
        family, sep, version = key.partition(":")
        family, version = family.strip().lower(), version.strip()
        if not sep or not family or not version:
            raise ValueError(f'{source}: key {key!r} must be "family:version"')
        if not isinstance(entry, dict) or not all(
                _is_price(entry.get(f)) for f in ("input", "output", "cache_read")):
            raise ValueError(f'{source}: {key!r} needs non-negative numbers '
                             f'"input", "output" and "cache_read"')
        table[(family, version)] = (float(entry["input"]), float(entry["output"]),
                                    float(entry["cache_read"]))
    return table


def load_prices(override: Optional[Union[str, os.PathLike]] = None
                ) -> Dict[Tuple[str, str], Tuple[float, float, float]]:
    """Built-in prices merged with a JSON override file. The override is `override`
    if given, otherwise the file named by $RUNLEDGER_PRICES. Override entries
    replace built-in entries with the same family and version."""
    default_text = resources.files(__package__).joinpath("prices.json").read_text(encoding="utf-8")
    prices = _parse_table(json.loads(default_text), "prices.json")
    path = override or os.environ.get(PRICES_ENV)
    if path:
        text = Path(path).read_text(encoding="utf-8")
        prices.update(_parse_table(json.loads(text), str(path)))
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
        s.cost = cost_of(s.usage, s.model)
    total = 0.0
    known = False
    for model, u in run.models.items():
        c = cost_of(u, model)
        if c is not None:
            total += c
            known = True
    run.cost = total if known else None
