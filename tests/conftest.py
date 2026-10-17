"""Shared pytest fixtures for RunLedger tests.

Isolates every test from the developer's real HOME/USERPROFILE and agent config
environment variables so the suite never reads real ~/.claude, ~/.factory, ~/.codex,
or ~/.runledger files.
"""
import pytest

ISOLATED_ENV_VARS = (
    "RUNLEDGER_SERVER",
    "RUNLEDGER_API_KEY",
    "RUNLEDGER_CONFIG",
    "FACTORY_HOME",
    "FACTORY_HOME_OVERRIDE",
    "CODEX_HOME",
    "CLAUDE_CONFIG_DIR",
    "FACTORY_PROJECT_DIR",
    "DROID_PROJECT_DIR",
    "CLAUDE_PROJECT_DIR",
)


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    """Redirect HOME and USERPROFILE to a fresh temporary directory and clear
    agent/RunLedger environment variables for every test."""
    home = tmp_path / "isolated-home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    for var in ISOLATED_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    return home
