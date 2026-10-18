"""Optional data retention for the self-hosted team server."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Dict, Mapping, Optional

from .db import TIME_FORMAT, Database

RETENTION_ENV = "RUNLEDGER_RETENTION_DAYS"
AUDIT_RETENTION_ENV = "RUNLEDGER_AUDIT_RETENTION_DAYS"
# The shortest run retention that can never delete a run of the month a budget alert belongs to.
RETENTION_FLOOR_DAYS = 35
# How long audit events are kept when runs have a retention period but the audit one is unset.
AUDIT_RETENTION_DEFAULT_DAYS = 365


def _whole_days(name: str, raw: str) -> int:
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{name} must be a whole number of days, not {raw!r}.") from None


def retention_from_env(environ: Optional[Mapping[str, str]] = None) -> Optional[int]:
    """RUNLEDGER_RETENTION_DAYS as a whole number of days, or None when unset. Values below
    RETENTION_FLOOR_DAYS are raised to it with a warning: a shorter period could delete the runs
    of the current month, which would drop the month's spend and fire its budget alerts again."""
    env = os.environ if environ is None else environ
    raw = (env.get(RETENTION_ENV) or "").strip()
    if not raw:
        return None
    days = _whole_days(RETENTION_ENV, raw)
    if days < 1:
        raise ValueError(f"{RETENTION_ENV} must be 1 or more.")
    if days < RETENTION_FLOOR_DAYS:
        sys.stderr.write(
            f"RunLedger: {RETENTION_ENV}={days} is below the {RETENTION_FLOOR_DAYS}-day floor "
            f"(budget alerts are kept per month); using {RETENTION_FLOOR_DAYS} days.\n"
        )
        return RETENTION_FLOOR_DAYS
    return days


def audit_retention_from_env(environ: Optional[Mapping[str, str]] = None) -> Optional[int]:
    """RUNLEDGER_AUDIT_RETENTION_DAYS as a whole number of days, or None when unset. 0 keeps
    audit events forever; the audit log is never deleted on the runs' schedule."""
    env = os.environ if environ is None else environ
    raw = (env.get(AUDIT_RETENTION_ENV) or "").strip()
    if not raw:
        return None
    days = _whole_days(AUDIT_RETENTION_ENV, raw)
    if days < 0:
        raise ValueError(f"{AUDIT_RETENTION_ENV} must be 0 (keep forever) or more.")
    return days


def run_maintenance(
    db: Database,
    retention_days: Optional[int],
    moment: Optional[datetime] = None,
    audit_days: Optional[int] = None,
) -> Dict[str, int]:
    """Delete history older than the configured retention periods; never delete teams or keys.

    `retention_days` covers runs, their versions, approvals and the budget alerts of earlier
    months; None keeps all of it. `audit_days` covers audit events only: 0 keeps them forever,
    and None means the default period while runs are being pruned, or "keep" when they are not.
    """
    if not retention_days and audit_days is None:
        return {}
    moment = moment or datetime.now(timezone.utc)
    run_cutoff = None
    if retention_days:
        run_cutoff = (moment - timedelta(days=retention_days)).astimezone(timezone.utc).strftime(TIME_FORMAT)
    if audit_days is None:
        audit_days = AUDIT_RETENTION_DEFAULT_DAYS if retention_days else 0
    audit_cutoff = None
    if audit_days:
        audit_cutoff = (moment - timedelta(days=audit_days)).astimezone(timezone.utc).strftime(TIME_FORMAT)
    return db.purge_before(run_cutoff, audit_cutoff)
