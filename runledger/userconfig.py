"""The user's own RunLedger config file: `$RUNLEDGER_CONFIG` or `~/.runledger/config.json`.

Every consumer reads it through this module (guard rules, risk policy, rollback settings,
doctor), so there is exactly one resolution of the path and one reading of the file.
Reading never raises: a missing, unreadable or invalid file is `{}`.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict

USER_CONFIG_DIR = ".runledger"
USER_CONFIG_FILE = "config.json"


def user_config_path() -> Path:
    """`RUNLEDGER_CONFIG` when set, else `~/.runledger/config.json`."""
    override = os.environ.get("RUNLEDGER_CONFIG", "").strip()
    if override:
        return Path(override)
    base = os.environ.get("HOME") or os.environ.get("USERPROFILE")
    return (Path(base) if base else Path.home()) / USER_CONFIG_DIR / USER_CONFIG_FILE


def load_user_config() -> Dict[str, Any]:
    """The user's config file as a dict, or {} when missing or invalid. Never raises."""
    try:
        data = json.loads(user_config_path().read_text(encoding="utf-8-sig"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}
