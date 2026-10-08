"""Claude API list prices in USD per million tokens.

Source: Anthropic pricing page (checked 2026-10-08). Update PRICES when it
changes; unknown models are reported with cost = None instead of a guess.
Cache writes are priced at the 5-minute rate (1.25x input).
"""
from __future__ import annotations

import re
from typing import Dict, Optional, Tuple

from .parser import Run, Usage

# (family, version) -> (input, output, cache_read)
PRICES: Dict[Tuple[str, str], Tuple[float, float, float]] = {
    ("haiku", "5.5"): (0.10, 0.50, 0.01),
    ("sonnet", "5.5"): (2.0, 10.0, 0.10),
    ("opus", "5.5"): (4.0, 20.0, 0.20),
    ("sonnet", "5"): (2.0, 10.0, 0.20),
    ("opus", "5"): (5.0, 25.0, 0.50),
    ("opus", "4.8"): (5.0, 25.0, 0.50),
    ("opus", "4.7"): (5.0, 25.0, 0.50),
    ("opus", "4.6"): (5.0, 25.0, 0.50),
    ("opus", "4.5"): (5.0, 25.0, 0.50),
    ("opus", "4.1"): (15.0, 75.0, 1.50),
    ("opus", "4"): (15.0, 75.0, 1.50),
    ("sonnet", "4.6"): (3.0, 15.0, 0.30),
    ("sonnet", "4.5"): (3.0, 15.0, 0.30),
    ("sonnet", "4"): (3.0, 15.0, 0.30),
    ("haiku", "4.5"): (1.0, 5.0, 0.10),
    ("haiku", "3.5"): (0.80, 4.0, 0.08),
    ("fable", "5.1"): (10.0, 50.0, 0.25),
    ("fable", "5"): (10.0, 50.0, 1.0),
    ("mythos", "5.1"): (10.0, 50.0, 0.25),
    ("mythos", "5"): (10.0, 50.0, 1.0),
}

_MODEL_RE = re.compile(r"claude-(?:(\d+)-(\d+)-)?([a-z]+)-(\d+)(?:-(\d{1,2}))?(?:-|$)")


def model_key(model: Optional[str]) -> Optional[Tuple[str, str]]:
    """'claude-sonnet-4-5-20250929' -> ('sonnet', '4.5');
    'claude-opus-4-20250514' -> ('opus', '4'); 'claude-3-5-haiku-x' -> ('haiku', '3.5')."""
    if not model:
        return None
    m = _MODEL_RE.search(model.lower())
    if not m:
        return None
    old_major, old_minor, family, major, minor = m.groups()
    if old_major:  # legacy naming claude-3-5-haiku-...
        return family, f"{old_major}.{old_minor}"
    version = f"{major}.{minor}" if minor else major
    return family, version


def friendly_model(model: Optional[str]) -> str:
    k = model_key(model)
    if not k:
        return model or "unknown"
    return f"{k[0].capitalize()} {k[1]}"


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
