"""Team budgets: a monthly spend limit for the team and one for each developer, the month's
spend, and the alerts sent when spend first crosses one of the threshold percentages.

Spend is the sum of run costs (list-price estimates, see pricing.py) for runs whose start
time, or push time when a run has no start, falls in the current calendar month in UTC.
A budget of null or 0 means no limit. Alerts are checked after every push (see app.py).
Each threshold fires once per team, or once per developer, per month: the budget_alerts
table has a unique key on (team, month, scope, threshold), and an alert is announced only
when its row is new.

Standard library only.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .db import TIME_FORMAT, Database

DEFAULT_THRESHOLDS = [50, 80, 100]
MAX_USD = 1_000_000_000
MAX_THRESHOLD = 500
MIN_THRESHOLDS = 1
MAX_THRESHOLDS = 3
FIELDS = ("monthly_usd", "per_user_monthly_usd", "alert_thresholds")


class InvalidBudget(ValueError):
    """A budget request failed validation. The message is safe to send to the client."""


def now() -> datetime:
    """The current time in UTC. Tests replace this function to move the clock."""
    return datetime.now(timezone.utc)


def month_window(when: datetime) -> Tuple[str, str, str]:
    """The UTC calendar month that contains `when`: ("YYYY-MM", start, end), with start and end
    as TIME_FORMAT text. A run belongs to the month when start <= its time < end."""
    moment = when.astimezone(timezone.utc)
    start = datetime(moment.year, moment.month, 1, tzinfo=timezone.utc)
    if moment.month == 12:
        end = datetime(moment.year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(moment.year, moment.month + 1, 1, tzinfo=timezone.utc)
    return f"{moment.year:04d}-{moment.month:02d}", start.strftime(TIME_FORMAT), end.strftime(TIME_FORMAT)


def _usd(name: str, value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise InvalidBudget(f"'{name}' must be a number of US dollars, or null.")
    if value < 0 or value > MAX_USD:
        raise InvalidBudget(f"'{name}' must be from 0 to {MAX_USD}.")
    return round(float(value), 6)


def _thresholds(value: Any) -> List[int]:
    rule = (
        f"'alert_thresholds' must be a list of {MIN_THRESHOLDS} to {MAX_THRESHOLDS} whole "
        f"numbers from 1 to {MAX_THRESHOLD}, in ascending order."
    )
    if not isinstance(value, list) or not MIN_THRESHOLDS <= len(value) <= MAX_THRESHOLDS:
        raise InvalidBudget(rule)
    out: List[int] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int) or not 1 <= item <= MAX_THRESHOLD:
            raise InvalidBudget(rule)
        if out and item <= out[-1]:
            raise InvalidBudget(rule)
        out.append(item)
    return out


def validate_budget(payload: Any) -> Dict[str, Any]:
    """Check a PUT /api/budgets body and return the values to store. The body replaces the whole
    budget: a missing amount means no limit, and a missing alert_thresholds means the defaults."""
    if not isinstance(payload, dict):
        raise InvalidBudget("The body must be a JSON object.")
    unknown = sorted(str(key)[:50] for key in payload if key not in FIELDS)
    if unknown:
        raise InvalidBudget(f"Unknown field '{unknown[0]}'. Use: {', '.join(FIELDS)}.")
    return {
        "monthly_usd": _usd("monthly_usd", payload.get("monthly_usd")),
        "per_user_monthly_usd": _usd("per_user_monthly_usd", payload.get("per_user_monthly_usd")),
        "alert_thresholds": _thresholds(payload.get("alert_thresholds", DEFAULT_THRESHOLDS)),
    }


def _pct(spend: float, limit: Optional[float]) -> Optional[float]:
    """Percent of the limit used, or None when there is no limit."""
    if limit is None or limit <= 0:
        return None
    return round(100.0 * spend / limit, 1)


def status(db: Database, team_id: int, when: Optional[datetime] = None) -> Dict[str, Any]:
    """This month's spend against the team budget, and each developer's spend, largest first."""
    month, start, end = month_window(when or now())
    settings = db.budget_settings(team_id)
    total, by_user = db.month_spend(team_id, start, end)
    per_user = [
        {"user": name, "spend_usd": round(spend, 6), "pct": _pct(spend, settings["per_user_monthly_usd"])}
        for name, spend in sorted(by_user, key=lambda item: (-item[1], item[0]))
    ]
    return {
        "month": month,
        "spend_usd": round(total, 6),
        "monthly_usd": settings["monthly_usd"],
        "pct": _pct(total, settings["monthly_usd"]),
        "per_user": per_user,
    }


def _crossed(month: str, scope: str, user: Optional[str], spend: float, limit: float,
             thresholds: List[int]) -> List[Dict[str, Any]]:
    pct = 100.0 * spend / limit
    return [
        {"scope": scope, "user": user, "threshold": t, "month": month,
         "spend_usd": round(spend, 6), "limit_usd": round(limit, 6), "pct": round(pct, 2)}
        for t in thresholds if pct >= t
    ]


def check_alerts(db: Database, team_id: int, when: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Record every threshold the team or a developer has crossed this month and has not yet
    alerted on. Returns only the newly fired alerts, for the caller to send."""
    month, start, end = month_window(when or now())
    settings = db.budget_settings(team_id)
    thresholds = settings["alert_thresholds"]
    total, by_user = db.month_spend(team_id, start, end)
    candidates: List[Dict[str, Any]] = []
    limit = settings["monthly_usd"]
    if limit:  # None and 0 both mean no limit
        candidates += _crossed(month, "team", None, total, limit, thresholds)
    per_user_limit = settings["per_user_monthly_usd"]
    if per_user_limit:
        for name, spend in by_user:
            candidates += _crossed(month, f"user:{name}", name, spend, per_user_limit, thresholds)
    if not candidates:
        return []
    return db.record_budget_alerts(team_id, month, candidates)
