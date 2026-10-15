"""Optional data retention for the self-hosted team server."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Dict, Mapping, Optional

from .db import TIME_FORMAT, Database

RETENTION_ENV = "RUNLEDGER_RETENTION_DAYS"


def retention_from_env(environ: Optional[Mapping[str, str]] = None) -> Optional[int]:
    """RUNLEDGER_RETENTION_DAYS as a whole number of days (1 or more), or None when unset."""
    env = os.environ if environ is None else environ
    raw = (env.get(RETENTION_ENV) or "").strip()
    if not raw:
        return None
    try:
        days = int(raw)
    except ValueError:
        raise ValueError(f"{RETENTION_ENV} must be a whole number of days, not {raw!r}.") from None
    if days < 1:
        raise ValueError(f"{RETENTION_ENV} must be 1 or more.")
    return days


def run_maintenance(
    db: Database, retention_days: Optional[int], moment: Optional[datetime] = None,
) -> Dict[str, int]:
    """Delete history older than the configured retention period; never delete teams or keys."""
    if not retention_days:
        return {}
    moment = moment or datetime.now(timezone.utc)
    cutoff = (moment - timedelta(days=retention_days)).astimezone(timezone.utc).strftime(TIME_FORMAT)
    return db.purge_before(cutoff)
