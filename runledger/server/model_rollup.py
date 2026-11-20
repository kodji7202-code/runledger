"""Per-receipt model buckets grouped by their display label."""
from typing import Any, Dict

from ..pricing import friendly_model


def receipt_models(models: Dict[str, Any], partial: bool = False) -> Dict[str, Dict[str, Any]]:
    """Merge aliases per receipt; partial means priced-but-incomplete cost."""
    grouped: Dict[str, Dict[str, Any]] = {}
    for name, info in models.items():
        info = info if isinstance(info, dict) else {}
        slot = grouped.setdefault(friendly_model(str(name)), {
            "tokens": 0, "cost_usd": None, "unpriced": False,
        })
        slot["tokens"] += int(info.get("tokens") or 0)
        cost = info.get("cost_usd")
        if cost is None:
            slot["unpriced"] = True
        else:
            slot["cost_usd"] = (slot["cost_usd"] or 0.0) + float(cost)
    for slot in grouped.values():
        unpriced = slot.pop("unpriced")
        slot["partial"] = partial or (unpriced and slot["cost_usd"] is not None)
    return grouped
