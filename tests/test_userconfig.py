"""The one reader of the user's config file (`RUNLEDGER_CONFIG` or `~/.runledger/config.json`).

Guard, risk policy and (from v0.7.0) rollback settings all consume this module, so a
broken file must never raise and the path must be resolved in exactly one way.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from runledger import userconfig

ROOT = Path(__file__).resolve().parent.parent


def test_path_defaults_to_the_home_directory(monkeypatch):
    monkeypatch.delenv("RUNLEDGER_CONFIG", raising=False)
    expected = Path(os.environ["HOME"]) / ".runledger" / "config.json"
    assert userconfig.user_config_path() == expected


def test_runledger_config_overrides_the_path(tmp_path, monkeypatch):
    target = tmp_path / "custom.json"
    monkeypatch.setenv("RUNLEDGER_CONFIG", str(target))
    assert userconfig.user_config_path() == target


@pytest.mark.parametrize("content", ["{bad", "[]", '"text"', "12"])
def test_load_returns_the_dict_and_never_raises(tmp_path, monkeypatch, content):
    path = tmp_path / "config.json"
    monkeypatch.setenv("RUNLEDGER_CONFIG", str(path))
    assert userconfig.load_user_config() == {}  # missing file
    path.write_text(json.dumps({"guard": {"mode": "monitor"}}), encoding="utf-8")
    assert userconfig.load_user_config() == {"guard": {"mode": "monitor"}}
    path.write_text(content, encoding="utf-8")
    assert userconfig.load_user_config() == {}


def test_a_directory_in_place_of_the_file_is_not_a_config(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.mkdir()
    monkeypatch.setenv("RUNLEDGER_CONFIG", str(path))
    assert userconfig.load_user_config() == {}


def test_the_loader_is_importable_without_the_guard():
    """rollback and doctor read user settings through this module without importing the
    guard and its rule engine, so importing it must not import runledger.guard."""
    code = ("import sys, runledger.userconfig as u; "
            "assert 'runledger.guard' not in sys.modules, 'guard was imported'; "
            "assert 'runledger.risk' not in sys.modules, 'risk was imported'; "
            "print(u.USER_CONFIG_DIR, u.USER_CONFIG_FILE)")
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                            cwd=str(ROOT), env={**os.environ, "PYTHONPATH": str(ROOT)})
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ".runledger config.json"


def test_guard_and_the_risk_policy_read_through_the_same_module(tmp_path, monkeypatch):
    from runledger import guard, risk

    path = tmp_path / "config.json"
    path.write_text(json.dumps({"ignore": ["Recursive force delete"]}), encoding="utf-8")
    monkeypatch.setenv("RUNLEDGER_CONFIG", str(path))

    assert userconfig.user_config_path() == path
    assert guard.user_config_path() == path
    assert guard.load_user_config() == {"ignore": ["Recursive force delete"]}
    assert risk.load_policy(None)["ignore"] == ["Recursive force delete"]
